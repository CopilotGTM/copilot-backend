"""Durable control-plane orchestration for provider-backed organisation signup."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import (
    AdminUserStatus,
    ExternalSignupProvider,
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
    UserAuthenticationMethod,
    UserEventType,
)
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProviderType,
)
from src.domain.models.user_signup_models import ExternalSignupRequest
from src.service.users.external_auth_service import ExternalSignupIdentity
from src.service.users.user_tenant_setup_service import TenantUserSnapshot


class SignupServiceError(ValueError):
    """Expose a stable signup outcome without leaking database details."""

    def __init__(self, message: str, *, code: str, status_code: int = 409):
        self.message = message
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class SignupService:
    """Own signup idempotency, control-plane creation, and monotonic milestones."""

    _ORDER = {
        SignupRequestStatus.RECEIVED: 0,
        SignupRequestStatus.CONTROL_PLANE_CREATED: 1,
        SignupRequestStatus.TENANT_DEFAULTS_CREATED: 2,
        SignupRequestStatus.STORAGE_CREATED: 3,
        SignupRequestStatus.ACTIVE: 4,
    }

    def __init__(self, session: AsyncSession):
        self._session = session

    async def start_signup(
        self,
        *,
        identity: ExternalSignupIdentity,
        request: ExternalSignupRequest,
        idempotency_key: str,
    ) -> SignupRequest:
        """Persist one provider/domain signup intent under transaction locks."""
        key_hash = sha256(idempotency_key.encode()).hexdigest()
        fingerprint = self._fingerprint(identity, request)
        claims = identity.claims

        # Serialize both ownership dimensions before any eligibility reads. The
        # database constraints remain the final guard if a caller skips this path.
        for lock_key in (
            f"signup-subject:{claims.mapping_provider.value}:{claims.provider_user_id}:{claims.provider_tenant_id}",
            f"signup-domain:{identity.primary_domain}",
        ):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )

        existing_key = (
            (
                await self._session.execute(
                    select(SignupRequest).where(col(SignupRequest.idempotency_key_hash) == key_hash)
                )
            )
            .scalars()
            .first()
        )
        if existing_key is not None:
            if existing_key.request_fingerprint != fingerprint:
                raise SignupServiceError(
                    "The idempotency key was already used for another request",
                    code="IDEMPOTENCY_KEY_REUSED",
                )
            return existing_key

        provider = self._signup_provider(claims.authentication_method)
        existing_subject = (
            (
                await self._session.execute(
                    select(SignupRequest).where(
                        col(SignupRequest.provider) == provider,
                        col(SignupRequest.provider_user_id) == claims.provider_user_id,
                        col(SignupRequest.provider_tenant_id) == claims.provider_tenant_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_subject is not None:
            if existing_subject.primary_domain != identity.primary_domain:
                raise SignupServiceError(
                    "This account is already creating another organisation",
                    code="ACCOUNT_IN_OTHER_ORGANISATION",
                )
            if existing_subject.status == SignupRequestStatus.RECEIVED:
                self._update_profile(existing_subject, request)
                await self._session.commit()
            return existing_subject

        existing_mapping = (
            (
                await self._session.execute(
                    select(ExternalIdentityMapping).where(
                        col(ExternalIdentityMapping.provider) == claims.mapping_provider,
                        col(ExternalIdentityMapping.external_user_id) == claims.provider_user_id,
                        col(ExternalIdentityMapping.external_tenant_id)
                        == claims.provider_tenant_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_mapping is not None:
            raise SignupServiceError(
                "This provider account already exists",
                code="ACCOUNT_ALREADY_EXISTS",
            )

        existing_user = (
            (
                await self._session.execute(
                    select(User).where(
                        or_(
                            col(User.supabase_user_id) == claims.supabase_user_id,
                            col(User.email) == claims.email.lower(),
                        )
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_user is not None:
            if existing_user.supabase_user_id == claims.supabase_user_id:
                raise SignupServiceError(
                    "This account already belongs to an organisation",
                    code="ACCOUNT_IN_OTHER_ORGANISATION",
                )
            raise SignupServiceError(
                "An account already exists for this identity",
                code="ACCOUNT_ALREADY_EXISTS",
            )

        existing_org = (
            (
                await self._session.execute(
                    select(Organisation).where(
                        col(Organisation.primary_domain) == identity.primary_domain
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_org is not None:
            raise SignupServiceError(
                "An organisation already owns this domain",
                code="ORGANISATION_ACCESS_REQUIRED",
            )

        existing_domain_signup = (
            (
                await self._session.execute(
                    select(SignupRequest).where(
                        col(SignupRequest.primary_domain) == identity.primary_domain,
                        col(SignupRequest.status) != SignupRequestStatus.FAILED,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_domain_signup is not None:
            raise SignupServiceError(
                "Another signup already reserved this organisation domain",
                code="ORGANISATION_ACCESS_REQUIRED",
            )

        signup = SignupRequest(
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            provider=provider,
            supabase_user_id=claims.supabase_user_id,
            provider_user_id=claims.provider_user_id,
            provider_tenant_id=claims.provider_tenant_id,
            normalized_email=claims.email.lower(),
            first_name=request.first_name,
            last_name=request.last_name,
            job_title=request.job_title,
            organisation_name=request.organisation_name,
            primary_domain=identity.primary_domain,
            country=request.country.upper(),
            sales_team_size=request.sales_team_size,
            website_url=str(request.website_url) if request.website_url else None,
            linkedin_url=str(request.linkedin_url) if request.linkedin_url else None,
            terms_version=request.terms_version,
            privacy_version=request.privacy_version,
        )
        self._session.add(signup)
        await self._session.commit()
        await self._session.refresh(signup)
        return signup

    async def ensure_control_plane(
        self,
        signup_request_id: UUID,
    ) -> tuple[SignupRequest, Organisation, User, TenantUserSnapshot]:
        """Atomically create or reload the organisation, founder, and identity."""
        signup = (
            (
                await self._session.execute(
                    select(SignupRequest)
                    .where(col(SignupRequest.signup_request_id) == signup_request_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if signup is None:
            raise SignupServiceError(
                "Signup request not found",
                code="SIGNUP_REQUEST_NOT_FOUND",
                status_code=404,
            )

        if signup.organisation_id is not None and signup.user_id is not None:
            organisation = await self._session.get(Organisation, signup.organisation_id)
            user = await self._session.get(User, signup.user_id)
            if organisation is None or user is None:
                raise SignupServiceError(
                    "Signup control-plane state is incomplete",
                    code="SIGNUP_RECONCILIATION_REQUIRED",
                    status_code=503,
                )
            if signup.status == SignupRequestStatus.RECONCILIATION_REQUIRED:
                # The external phases are idempotent, so reconciliation restarts
                # from the last atomic master milestone rather than guessing which
                # provider call completed before a timeout.
                signup.status = SignupRequestStatus.CONTROL_PLANE_CREATED
                await self._session.commit()
            return signup, organisation, user, self._snapshot(user)

        now = datetime.now(UTC)
        organisation = Organisation(
            organisation_name=signup.organisation_name,
            domain=[signup.primary_domain],
            primary_domain=signup.primary_domain,
            domain_verification_method=signup.provider.value,
            domain_verified_at=now,
            country=signup.country,
            sales_team_size=signup.sales_team_size,
            website_urls=[signup.website_url] if signup.website_url else [],
            linked_urls=[signup.linkedin_url] if signup.linkedin_url else [],
            status=OrganisationStatus.PROVISIONING,
            created_by_auth_user_id=signup.supabase_user_id,
        )
        user = User(
            organisation_id=organisation.organisation_id,
            supabase_user_id=signup.supabase_user_id,
            email=signup.normalized_email,
            first_name=signup.first_name,
            last_name=signup.last_name,
            job_title=signup.job_title,
            user_role=UserAppRole.ADMIN,
            organisation_role="Administrator",
            authentication_method=self._authentication_method(signup.provider),
            profile_image_url=None,
            status=SupabaseUserStatus.ACTIVE,
            admin_status=AdminUserStatus.APPROVED,
            email_verified=True,
            created_by_auth_user_id=signup.supabase_user_id,
            approved_at=now,
        )
        mapping = ExternalIdentityMapping(
            user_id=user.user_id,
            organisation_id=organisation.organisation_id,
            provider=(self._mapping_provider(signup.provider)),
            provider_type=ExternalIdentityProviderType.OAUTH,
            external_user_id=signup.provider_user_id,
            external_tenant_id=signup.provider_tenant_id,
            external_user_email=signup.normalized_email,
            external_user_name=f"{signup.first_name} {signup.last_name}".strip(),
            linked_at=now,
            is_active=True,
        )
        request_id = str(signup.signup_request_id)
        self._session.add(organisation)
        self._session.add(user)
        self._session.add(mapping)
        self._session.add(
            OrganisationActionHistory(
                organisation_id=organisation.organisation_id,
                action=OrganisationEventType.CREATED,
                request_id=request_id,
                details={"source": "self_service_signup"},
            )
        )
        self._session.add(
            UserActionHistory(
                user_id=user.user_id,
                action=UserEventType.CREATED,
                request_id=request_id,
                details={"source": "self_service_signup", "role": "admin"},
            )
        )
        signup.organisation_id = organisation.organisation_id
        signup.user_id = user.user_id
        signup.status = SignupRequestStatus.CONTROL_PLANE_CREATED
        signup.last_error_code = None
        signup.last_error_at = None
        await self._session.commit()
        return signup, organisation, user, self._snapshot(user)

    async def mark_tenant_defaults_created(self, signup_request_id: UUID) -> SignupRequest:
        """Advance a request after founder CRM defaults commit."""
        return await self._advance(
            signup_request_id,
            SignupRequestStatus.CONTROL_PLANE_CREATED,
            SignupRequestStatus.TENANT_DEFAULTS_CREATED,
        )

    async def mark_storage_created(self, signup_request_id: UUID) -> SignupRequest:
        """Advance a request after strict Azure container provisioning succeeds."""
        return await self._advance(
            signup_request_id,
            SignupRequestStatus.TENANT_DEFAULTS_CREATED,
            SignupRequestStatus.STORAGE_CREATED,
        )

    async def activate(self, signup_request_id: UUID) -> SignupRequest:
        """Atomically activate both durable signup and organisation state."""
        signup = await self._locked_request(signup_request_id)
        if signup.status == SignupRequestStatus.ACTIVE:
            return signup
        if signup.status != SignupRequestStatus.STORAGE_CREATED or signup.organisation_id is None:
            raise SignupServiceError(
                "Signup is not ready for activation",
                code="SIGNUP_STATE_INVALID",
            )
        organisation = await self._session.get(Organisation, signup.organisation_id)
        if organisation is None:
            raise SignupServiceError(
                "Organisation not found",
                code="SIGNUP_RECONCILIATION_REQUIRED",
                status_code=503,
            )
        organisation.status = OrganisationStatus.APPROVED
        organisation.approved_at = datetime.now(UTC)
        signup.status = SignupRequestStatus.ACTIVE
        signup.completed_at = datetime.now(UTC)
        signup.last_error_code = None
        signup.last_error_at = None
        await self._session.commit()
        return signup

    async def mark_reconciliation_required(
        self,
        signup_request_id: UUID,
        error_code: str,
    ) -> SignupRequest:
        """Persist a retryable local failure code without exception text."""
        signup = await self._locked_request(signup_request_id)
        if signup.status == SignupRequestStatus.ACTIVE:
            return signup
        signup.status = SignupRequestStatus.RECONCILIATION_REQUIRED
        signup.attempt_count += 1
        signup.last_error_code = error_code
        signup.last_error_at = datetime.now(UTC)
        await self._session.commit()
        return signup

    async def get_owned_request(
        self,
        signup_request_id: UUID,
        identity: ExternalSignupIdentity,
    ) -> SignupRequest | None:
        """Return status only to the immutable provider identity that created it."""
        return (
            (
                await self._session.execute(
                    select(SignupRequest).where(
                        col(SignupRequest.signup_request_id) == signup_request_id,
                        col(SignupRequest.supabase_user_id) == identity.claims.supabase_user_id,
                        col(SignupRequest.provider_user_id) == identity.claims.provider_user_id,
                        col(SignupRequest.provider_tenant_id) == identity.claims.provider_tenant_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    async def _advance(
        self,
        signup_request_id: UUID,
        expected: SignupRequestStatus,
        target: SignupRequestStatus,
    ) -> SignupRequest:
        signup = await self._locked_request(signup_request_id)
        current_order = self._ORDER.get(signup.status)
        if current_order is not None and current_order >= self._ORDER[target]:
            return signup
        if signup.status != expected:
            raise SignupServiceError(
                "Signup state transition is invalid",
                code="SIGNUP_STATE_INVALID",
            )
        signup.status = target
        signup.last_error_code = None
        signup.last_error_at = None
        await self._session.commit()
        return signup

    async def _locked_request(self, signup_request_id: UUID) -> SignupRequest:
        signup = (
            (
                await self._session.execute(
                    select(SignupRequest)
                    .where(col(SignupRequest.signup_request_id) == signup_request_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if signup is None:
            raise SignupServiceError(
                "Signup request not found",
                code="SIGNUP_REQUEST_NOT_FOUND",
                status_code=404,
            )
        return signup

    @staticmethod
    def _fingerprint(
        identity: ExternalSignupIdentity,
        request: ExternalSignupRequest,
    ) -> str:
        payload = {
            "provider_subject": identity.claims.provider_user_id,
            "provider_tenant": identity.claims.provider_tenant_id,
            "request": request.model_dump(mode="json"),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _update_profile(signup: SignupRequest, request: ExternalSignupRequest) -> None:
        signup.first_name = request.first_name
        signup.last_name = request.last_name
        signup.job_title = request.job_title
        signup.organisation_name = request.organisation_name
        signup.country = request.country.upper()
        signup.sales_team_size = request.sales_team_size
        signup.website_url = str(request.website_url) if request.website_url else None
        signup.linkedin_url = str(request.linkedin_url) if request.linkedin_url else None
        signup.terms_version = request.terms_version
        signup.privacy_version = request.privacy_version

    @staticmethod
    def _signup_provider(method: UserAuthenticationMethod) -> ExternalSignupProvider:
        if method == UserAuthenticationMethod.GOOGLE_WORKSPACE:
            return ExternalSignupProvider.GOOGLE_WORKSPACE
        if method == UserAuthenticationMethod.MICROSOFT_ENTRA:
            return ExternalSignupProvider.MICROSOFT_ENTRA
        raise SignupServiceError(
            "Unsupported signup provider",
            code="SIGNUP_IDENTITY_INVALID",
            status_code=400,
        )

    @staticmethod
    def _authentication_method(provider: ExternalSignupProvider) -> UserAuthenticationMethod:
        return UserAuthenticationMethod(provider.value)

    @staticmethod
    def _mapping_provider(provider: ExternalSignupProvider):
        from src.domain.models.master_tables.external_identity_models import (
            ExternalIdentityProvider,
        )

        return (
            ExternalIdentityProvider.GOOGLE
            if provider == ExternalSignupProvider.GOOGLE_WORKSPACE
            else ExternalIdentityProvider.AZURE_AD
        )

    @staticmethod
    def _snapshot(user: User) -> TenantUserSnapshot:
        return TenantUserSnapshot(
            user_id=user.user_id,
            organisation_id=user.organisation_id,
            email=str(user.email),
            first_name=user.first_name,
            last_name=user.last_name,
            user_role=user.user_role.value,
            job_title=user.job_title,
            is_active=user.status == SupabaseUserStatus.ACTIVE,
        )
