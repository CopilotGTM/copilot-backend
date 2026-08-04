"""Idempotent member admission for verified existing-organisation identities."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.membership_models import OrganisationMemberInvitationRequest
from src.domain.models.master_tables import (
    AdminUserStatus,
    Organisation,
    OrganisationActionHistory,
    OrganisationEventType,
    OrganisationStatus,
    SignupRequest,
    SignupRequestStatus,
    SupabaseUserStatus,
    User,
    UserActionHistory,
    UserAppRole,
    UserEventType,
)
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProviderType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.service.orgnisation.organisation_preferences import (
    DOMAIN_JOIN_POLICY_INVITE_ONLY,
    normalize_organisation_preferences,
)
from src.service.users.external_auth_service import ExternalAuthService, ExternalSignupIdentity
from src.service.users.signup_service import SignupService
from src.service.users.user_tenant_setup_service import TenantUserSnapshot, UserTenantSetupService


@dataclass(frozen=True)
class MembershipResult:
    signup: SignupRequest
    status: SignupRequestStatus
    user: User | None
    mapping: ExternalIdentityMapping | None
    notify_admins: bool


class OrganisationMembershipError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 409) -> None:
        self.message = message
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class OrganisationMembershipService:
    """Create a fixed-role member or persist that an invitation is required."""

    def __init__(self, master_session: AsyncSession) -> None:
        self._session = master_session

    async def invite_member(
        self,
        *,
        organisation: Organisation,
        inviter: User,
        request: OrganisationMemberInvitationRequest,
    ) -> User:
        """Create an approved member invitation regardless of domain join policy."""
        if (
            inviter.organisation_id != organisation.organisation_id
            or inviter.user_role != UserAppRole.ADMIN
        ):
            raise OrganisationMembershipError(
                "Organisation admin access required",
                code="ORGANISATION_ADMIN_REQUIRED",
                status_code=403,
            )
        if not organisation.is_active or organisation.status != OrganisationStatus.APPROVED:
            raise OrganisationMembershipError(
                "Organisation is not available",
                code="ORGANISATION_INACTIVE",
                status_code=403,
            )

        domains = [
            domain
            for domain in [organisation.primary_domain, *(organisation.domain or [])]
            if domain
        ]
        normalized_email = str(request.email).strip().lower()
        if not ExternalAuthService().email_matches_organisation_domain(
            email=normalized_email,
            organisation_domains=domains,
        ):
            raise OrganisationMembershipError(
                "Use a company email that matches the organisation domain",
                code="INVALID_COMPANY_EMAIL_DOMAIN",
                status_code=422,
            )

        await SignupService(self._session).lock_email(normalized_email)
        existing_user = (
            (
                await self._session.execute(
                    select(User).where(func.lower(col(User.email)) == normalized_email)
                )
            )
            .scalars()
            .first()
        )
        if existing_user is not None:
            if (
                existing_user.organisation_id == organisation.organisation_id
                and existing_user.user_role == UserAppRole.MEMBER
                and existing_user.supabase_user_id is None
                and existing_user.authentication_method == request.authentication_method
            ):
                return existing_user
            raise OrganisationMembershipError(
                "A user already exists for this email",
                code="USER_ALREADY_EXISTS",
            )

        now = datetime.now(UTC)
        user = User(
            organisation_id=organisation.organisation_id,
            email=normalized_email,
            first_name=request.first_name,
            last_name=request.last_name,
            user_role=UserAppRole.MEMBER,
            organisation_role="Member",
            authentication_method=request.authentication_method,
            status=SupabaseUserStatus.ACTIVE,
            admin_status=AdminUserStatus.APPROVED,
            email_verified=False,
            created_by_auth_user_id=inviter.supabase_user_id,
            approved_at=now,
        )
        details = {
            "source": "organisation_admin_invitation",
            "actor_user_id": str(inviter.user_id),
        }
        request_id = f"invitation:{user.user_id}"
        self._session.add(user)
        self._session.add(
            UserActionHistory(
                user_id=user.user_id,
                action=UserEventType.CREATED,
                request_id=request_id,
                details=details,
            )
        )
        self._session.add(
            OrganisationActionHistory(
                organisation_id=organisation.organisation_id,
                action=OrganisationEventType.USER_ADDED,
                request_id=request_id,
                details={**details, "user_id": str(user.user_id)},
            )
        )
        await self._session.flush()
        crm_user_created = await self._ensure_tenant_user(user)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            if crm_user_created:
                await self._delete_tenant_user(user)
            raise
        return user

    async def join_verified_identity(
        self,
        identity: ExternalSignupIdentity,
        signup_request_id: UUID,
    ) -> MembershipResult:
        signup_service = SignupService(self._session)
        await signup_service.lock_subject(identity)
        await signup_service.lock_email(identity.claims.email)
        signup = await signup_service.get_owned_request_for_update(signup_request_id, identity)
        if signup is None:
            raise OrganisationMembershipError(
                "Signup request not found",
                code="SIGNUP_REQUEST_NOT_FOUND",
                status_code=404,
            )
        if signup.status == SignupRequestStatus.ACTIVE:
            return await self._existing_result(signup, identity)
        if signup.organisation_id is None or signup.status not in {
            SignupRequestStatus.DRAFT,
            SignupRequestStatus.INVITATION_REQUIRED,
        }:
            raise OrganisationMembershipError(
                "Signup request cannot join an existing organisation",
                code="SIGNUP_FLOW_INVALID",
            )

        organisation = (
            (
                await self._session.execute(
                    select(Organisation)
                    .where(col(Organisation.organisation_id) == signup.organisation_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if (
            organisation is None
            or not organisation.is_active
            or organisation.status != OrganisationStatus.APPROVED
        ):
            raise OrganisationMembershipError(
                "Organisation is not available",
                code="ORGANISATION_INACTIVE",
                status_code=403,
            )

        domains = [
            domain
            for domain in [organisation.primary_domain, *(organisation.domain or [])]
            if domain
        ]
        if (
            identity.primary_domain != signup.primary_domain
            or not ExternalAuthService().email_matches_organisation_domain(
                email=identity.claims.email,
                organisation_domains=domains,
            )
        ):
            raise OrganisationMembershipError(
                "The verified identity no longer matches this organisation",
                code="SIGNUP_IDENTITY_INVALID",
                status_code=403,
            )

        policy = normalize_organisation_preferences(organisation.preferences)["membership"]
        if policy["domain_join_policy"] == DOMAIN_JOIN_POLICY_INVITE_ONLY:
            notify_admins = signup.status != SignupRequestStatus.INVITATION_REQUIRED
            signup.status = SignupRequestStatus.INVITATION_REQUIRED
            await self._session.commit()
            return MembershipResult(signup, signup.status, None, None, notify_admins)

        await signup_service.ensure_identity_available(identity)
        now = datetime.now(UTC)
        user = User(
            organisation_id=organisation.organisation_id,
            supabase_user_id=identity.claims.supabase_user_id,
            email=identity.claims.email.lower(),
            first_name=signup.first_name,
            last_name=signup.last_name,
            user_role=UserAppRole.MEMBER,
            organisation_role="Member",
            authentication_method=identity.claims.authentication_method,
            profile_image_url=signup.profile_image_url,
            preferences={"profile_incomplete": True} if signup.profile_incomplete else {},
            status=SupabaseUserStatus.ACTIVE,
            admin_status=AdminUserStatus.APPROVED,
            email_verified=True,
            created_by_auth_user_id=identity.claims.supabase_user_id,
            approved_at=now,
        )
        mapping = ExternalIdentityMapping(
            user_id=user.user_id,
            organisation_id=organisation.organisation_id,
            provider=identity.claims.mapping_provider,
            provider_type=ExternalIdentityProviderType.OAUTH,
            external_user_id=identity.claims.provider_user_id,
            external_tenant_id=identity.claims.provider_tenant_id,
            external_user_email=identity.claims.email.lower(),
            external_user_name=f"{signup.first_name} {signup.last_name}".strip(),
            linked_at=now,
            is_active=True,
        )
        request_id = f"membership:{signup.signup_request_id}"
        self._session.add(user)
        self._session.add(mapping)
        self._session.add(
            UserActionHistory(
                user_id=user.user_id,
                action=UserEventType.CREATED,
                request_id=request_id,
                details={"source": "domain_auto_join"},
            )
        )
        self._session.add(
            OrganisationActionHistory(
                organisation_id=organisation.organisation_id,
                action=OrganisationEventType.USER_ADDED,
                request_id=request_id,
                details={"source": "domain_auto_join", "user_id": str(user.user_id)},
            )
        )
        await self._session.flush()

        crm_user_created = await self._ensure_tenant_user(user)
        signup.user_id = user.user_id
        signup.status = SignupRequestStatus.ACTIVE
        signup.completed_at = now
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            if crm_user_created:
                await self._delete_tenant_user(user)
            raise
        return MembershipResult(signup, signup.status, user, mapping, True)

    async def _existing_result(
        self,
        signup: SignupRequest,
        identity: ExternalSignupIdentity,
    ) -> MembershipResult:
        if signup.user_id is None:
            raise OrganisationMembershipError(
                "Completed membership is incomplete",
                code="SIGNUP_RECONCILIATION_REQUIRED",
                status_code=503,
            )
        user = await self._session.get(User, signup.user_id)
        mapping = (
            (
                await self._session.execute(
                    select(ExternalIdentityMapping).where(
                        col(ExternalIdentityMapping.user_id) == signup.user_id,
                        col(ExternalIdentityMapping.provider) == identity.claims.mapping_provider,
                        col(ExternalIdentityMapping.external_user_id)
                        == identity.claims.provider_user_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if user is None or mapping is None:
            raise OrganisationMembershipError(
                "Completed membership is incomplete",
                code="SIGNUP_RECONCILIATION_REQUIRED",
                status_code=503,
            )
        return MembershipResult(signup, signup.status, user, mapping, False)

    async def _ensure_tenant_user(self, user: User) -> bool:
        async for session in acquire_org_async_session(user.organisation_id):
            result = await UserTenantSetupService(session).ensure_user(
                TenantUserSnapshot(
                    user_id=user.user_id,
                    organisation_id=user.organisation_id,
                    email=str(user.email),
                    first_name=user.first_name,
                    last_name=user.last_name,
                    user_role=user.user_role.value,
                    job_title=user.job_title,
                    is_active=True,
                )
            )
            return result.created
        return False

    async def _delete_tenant_user(self, user: User) -> None:
        async for session in acquire_org_async_session(user.organisation_id):
            await session.execute(
                delete(CRMUser).where(
                    col(CRMUser.user_id) == user.user_id,
                    col(CRMUser.organisation_id) == user.organisation_id,
                )
            )
            await session.commit()
            return
