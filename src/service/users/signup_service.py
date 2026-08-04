"""Durable control-plane orchestration for provider-backed organisation signup."""

import json
import os
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

# These versions identify the legal text accepted by the Create workspace action.
# Deployments can advance them without allowing the browser to choose stale text.
SIGNUP_TERMS_VERSION = os.getenv("SIGNUP_TERMS_VERSION", "2026-08-04").strip() or "2026-08-04"
SIGNUP_PRIVACY_VERSION = os.getenv("SIGNUP_PRIVACY_VERSION", "2026-08-04").strip() or "2026-08-04"


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
        SignupRequestStatus.DRAFT: -1,
        SignupRequestStatus.RECEIVED: 0,
        SignupRequestStatus.CONTROL_PLANE_CREATED: 1,
        SignupRequestStatus.TENANT_DEFAULTS_CREATED: 2,
        SignupRequestStatus.STORAGE_CREATED: 3,
        SignupRequestStatus.ACTIVE: 4,
    }

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_or_create_draft(
        self,
        *,
        identity: ExternalSignupIdentity,
    ) -> SignupRequest:
        """Create or resume the verified subject's draft without reserving its domain.

        Called by signup preflight after provider verification. A draft records only
        resumable identity context; organisation ownership is checked on submission.
        """
        await self._lock_subject(identity)
        existing_subject = await self._subject_request(identity)
        if existing_subject is not None:
            return existing_subject

        await self._ensure_identity_available(identity)
        signup = self._new_draft(identity)
        self._session.add(signup)
        await self._session.commit()
        await self._session.refresh(signup)
        return signup

    async def finalize_signup(
        self,
        *,
        identity: ExternalSignupIdentity,
        request: ExternalSignupRequest,
    ) -> SignupRequest:
        """Finalize one draft and return its durable provisioning state.

        The provider subject and company domain are serialized before ownership
        checks. Repeated submissions return the same request so the HTTP layer can
        safely retry queue dispatch until a worker advances the state.
        """
        await self._lock_subject(identity)
        signup = await self._subject_request(identity)
        if signup is not None and signup.status != SignupRequestStatus.DRAFT:
            return signup
        if signup is None:
            signup = self._new_draft(identity)
            self._session.add(signup)

        # Drafts do not reserve domains. The domain lock begins only when the
        # founder submits Create workspace, so abandoned tabs cannot block peers.
        await self._lock(f"signup-domain:{identity.primary_domain}")
        await self._ensure_identity_available(identity)
        self._refresh_draft_identity(signup, identity)

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
                        col(SignupRequest.signup_request_id) != signup.signup_request_id,
                        col(SignupRequest.status).not_in(
                            [SignupRequestStatus.DRAFT, SignupRequestStatus.FAILED]
                        ),
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

        signup.organisation_name = request.organisation_name
        signup.request_fingerprint = self._fingerprint(identity, request)
        signup.terms_version = SIGNUP_TERMS_VERSION
        signup.privacy_version = SIGNUP_PRIVACY_VERSION
        signup.status = SignupRequestStatus.RECEIVED
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

        if signup.status == SignupRequestStatus.DRAFT:
            raise SignupServiceError(
                "Signup has not been submitted",
                code="SIGNUP_STATE_INVALID",
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
            profile_image_url=signup.profile_image_url,
            preferences={"profile_incomplete": True} if signup.profile_incomplete else {},
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

    async def _lock_subject(self, identity: ExternalSignupIdentity) -> None:
        claims = identity.claims
        await self._lock(
            f"signup-subject:{claims.mapping_provider.value}:"
            f"{claims.provider_user_id}:{claims.provider_tenant_id}"
        )

    async def _lock(self, lock_key: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    async def _subject_request(
        self,
        identity: ExternalSignupIdentity,
    ) -> SignupRequest | None:
        claims = identity.claims
        provider = self._signup_provider(claims.authentication_method)
        return (
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

    async def _ensure_identity_available(self, identity: ExternalSignupIdentity) -> None:
        claims = identity.claims
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
        if existing_user is None:
            return
        if existing_user.supabase_user_id == claims.supabase_user_id:
            raise SignupServiceError(
                "This account already belongs to an organisation",
                code="ACCOUNT_IN_OTHER_ORGANISATION",
            )
        raise SignupServiceError(
            "An account already exists for this identity",
            code="ACCOUNT_ALREADY_EXISTS",
        )

    def _new_draft(self, identity: ExternalSignupIdentity) -> SignupRequest:
        claims = identity.claims
        provider = self._signup_provider(claims.authentication_method)
        first_name, last_name, profile_incomplete = self._founder_profile(identity)
        identity_key = f"{provider.value}:{claims.provider_user_id}:{claims.provider_tenant_id}"
        return SignupRequest(
            idempotency_key_hash=sha256(f"signup-draft:{identity_key}".encode()).hexdigest(),
            request_fingerprint=sha256(identity_key.encode()).hexdigest(),
            provider=provider,
            supabase_user_id=claims.supabase_user_id,
            provider_user_id=claims.provider_user_id,
            provider_tenant_id=claims.provider_tenant_id,
            normalized_email=claims.email.lower(),
            first_name=first_name,
            last_name=last_name,
            organisation_name=None,
            primary_domain=identity.primary_domain,
            profile_image_url=(
                claims.profile_image_url
                if claims.profile_image_url and len(claims.profile_image_url) <= 1000
                else None
            ),
            profile_incomplete=profile_incomplete,
            status=SignupRequestStatus.DRAFT,
        )

    def _refresh_draft_identity(
        self,
        signup: SignupRequest,
        identity: ExternalSignupIdentity,
    ) -> None:
        """Refresh non-editable profile data from the final verified session."""
        claims = identity.claims
        first_name, last_name, profile_incomplete = self._founder_profile(identity)
        signup.supabase_user_id = claims.supabase_user_id
        signup.normalized_email = claims.email.lower()
        signup.first_name = first_name
        signup.last_name = last_name
        signup.primary_domain = identity.primary_domain
        signup.profile_image_url = (
            claims.profile_image_url
            if claims.profile_image_url and len(claims.profile_image_url) <= 1000
            else None
        )
        signup.profile_incomplete = profile_incomplete

    @staticmethod
    def _founder_profile(identity: ExternalSignupIdentity) -> tuple[str, str, bool]:
        full_name = (identity.claims.full_name or "").strip()
        if not full_name:
            return identity.claims.email.split("@", 1)[0][:100], "", True
        parts = full_name.split(maxsplit=1)
        first_name = parts[0][:100]
        last_name = parts[1][:100] if len(parts) == 2 else ""
        return first_name, last_name, not bool(last_name)

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
