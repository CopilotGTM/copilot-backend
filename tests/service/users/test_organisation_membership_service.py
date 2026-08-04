"""Existing-domain signup creates members safely and idempotently."""

from types import SimpleNamespace

import pytest
from sqlalchemy.sql.elements import TextClause
from src.api.schemas.membership_models import OrganisationMemberInvitationRequest
from src.domain.models.master_tables import (
    AdminUserStatus,
    ExternalSignupProvider,
    Organisation,
    OrganisationStatus,
    SignupRequest,
    SignupRequestStatus,
    SupabaseUserStatus,
    User,
    UserAppRole,
    UserAuthenticationMethod,
)
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProvider,
    ExternalIdentityProviderType,
)
from src.service.users.external_auth_service import ExternalAuthClaims, ExternalSignupIdentity
from src.service.users.organisation_membership_service import (
    OrganisationMembershipError,
    OrganisationMembershipService,
)


class _Scalars:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalars(self):
        return _Scalars(self.value)


class _MasterSession:
    def __init__(self, query_results=(), rows=None, *, fail_commit=False):
        self.query_results = list(query_results)
        self.rows = rows or {}
        self.fail_commit = fail_commit
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, _parameters=None):
        if isinstance(statement, TextClause):
            return _Result()
        return _Result(self.query_results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("master commit failed")

    async def rollback(self):
        self.rollbacks += 1

    async def get(self, model, identifier):
        return self.rows.get((model, identifier))


class _TenantSession:
    def __init__(self):
        self.deleted = 0
        self.commits = 0

    async def execute(self, _statement, _parameters=None):
        self.deleted += 1
        return _Result()

    async def commit(self):
        self.commits += 1


def _identity(
    provider="google",
    *,
    email="teammate@acme.com",
    primary_domain="acme.com",
) -> ExternalSignupIdentity:
    microsoft = provider == "microsoft"
    claims = ExternalAuthClaims(
        authentication_method=(
            UserAuthenticationMethod.MICROSOFT_ENTRA
            if microsoft
            else UserAuthenticationMethod.GOOGLE_WORKSPACE
        ),
        mapping_provider=(
            ExternalIdentityProvider.AZURE_AD if microsoft else ExternalIdentityProvider.GOOGLE
        ),
        supabase_user_id="supabase-user",
        provider_user_id="provider-user",
        provider_tenant_id=("00000000-0000-4000-8000-000000000001" if microsoft else "acme.com"),
        email=email,
        full_name="Team Member",
        profile_image_url=None,
    )
    return ExternalSignupIdentity(claims=claims, primary_domain=primary_domain)


def _organisation(policy="open") -> Organisation:
    return Organisation(
        organisation_name="Acme",
        primary_domain="acme.com",
        preferences={"membership": {"domain_join_policy": policy}},
        status=OrganisationStatus.APPROVED,
    )


def _signup(organisation: Organisation, **overrides) -> SignupRequest:
    values = {
        "idempotency_key_hash": "a" * 64,
        "request_fingerprint": "b" * 64,
        "provider": ExternalSignupProvider.GOOGLE_WORKSPACE,
        "supabase_user_id": "supabase-user",
        "provider_user_id": "provider-user",
        "provider_tenant_id": "acme.com",
        "normalized_email": "teammate@acme.com",
        "first_name": "Team",
        "last_name": "Member",
        "primary_domain": "acme.com",
        "status": SignupRequestStatus.DRAFT,
        "organisation_id": organisation.organisation_id,
    }
    values.update(overrides)
    return SignupRequest(**values)


def _patch_tenant(monkeypatch, tenant_session):
    async def acquire(_organisation_id):
        yield tenant_session

    async def ensure_user(_self, _snapshot, **_kwargs):
        return SimpleNamespace(created=True)

    monkeypatch.setattr(
        "src.service.users.organisation_membership_service.acquire_org_async_session",
        acquire,
    )
    monkeypatch.setattr(
        "src.service.users.organisation_membership_service.UserTenantSetupService.ensure_user",
        ensure_user,
    )


@pytest.mark.asyncio
async def test_open_domain_join_creates_member_mapping_and_audit(monkeypatch) -> None:
    organisation = _organisation()
    signup = _signup(organisation)
    master = _MasterSession([signup, organisation, None, None])
    _patch_tenant(monkeypatch, _TenantSession())

    result = await OrganisationMembershipService(master).join_verified_identity(
        _identity(), signup.signup_request_id
    )

    assert result.status == SignupRequestStatus.ACTIVE
    assert result.user is not None
    assert result.user.user_role == UserAppRole.MEMBER
    assert result.user.admin_status == AdminUserStatus.APPROVED
    assert result.user.status == SupabaseUserStatus.ACTIVE
    assert result.user.organisation_id == organisation.organisation_id
    assert result.mapping is not None
    assert result.mapping.external_user_id == "provider-user"
    assert result.mapping.provider_type == ExternalIdentityProviderType.OAUTH
    assert result.signup.user_id == result.user.user_id
    assert result.notify_admins is True


@pytest.mark.asyncio
async def test_join_rejects_provider_identity_that_changed_domain() -> None:
    organisation = _organisation()
    signup = _signup(
        organisation,
        provider=ExternalSignupProvider.MICROSOFT_ENTRA,
        provider_tenant_id="00000000-0000-4000-8000-000000000001",
    )
    master = _MasterSession([signup, organisation])

    with pytest.raises(OrganisationMembershipError) as error:
        await OrganisationMembershipService(master).join_verified_identity(
            _identity(
                "microsoft",
                email="teammate@other.com",
                primary_domain="other.com",
            ),
            signup.signup_request_id,
        )

    assert error.value.code == "SIGNUP_IDENTITY_INVALID"
    assert master.added == []


@pytest.mark.asyncio
async def test_invite_only_join_creates_no_user_or_mapping() -> None:
    organisation = _organisation("invite_only")
    signup = _signup(organisation)
    master = _MasterSession([signup, organisation])

    result = await OrganisationMembershipService(master).join_verified_identity(
        _identity(), signup.signup_request_id
    )

    assert result.status == SignupRequestStatus.INVITATION_REQUIRED
    assert result.user is None
    assert result.mapping is None
    assert result.notify_admins is True
    assert not any(isinstance(item, User) for item in master.added)
    assert not any(isinstance(item, ExternalIdentityMapping) for item in master.added)


@pytest.mark.asyncio
async def test_repeated_invitation_required_join_notifies_admins_once() -> None:
    organisation = _organisation("invite_only")
    signup = _signup(organisation)
    master = _MasterSession([signup, organisation, signup, organisation])
    service = OrganisationMembershipService(master)

    first = await service.join_verified_identity(_identity(), signup.signup_request_id)
    second = await service.join_verified_identity(_identity(), signup.signup_request_id)

    assert first.notify_admins is True
    assert second.notify_admins is False
    assert second.status == SignupRequestStatus.INVITATION_REQUIRED


@pytest.mark.asyncio
async def test_microsoft_join_uses_azure_mapping(monkeypatch) -> None:
    organisation = _organisation()
    signup = _signup(
        organisation,
        provider=ExternalSignupProvider.MICROSOFT_ENTRA,
        provider_tenant_id="00000000-0000-4000-8000-000000000001",
    )
    master = _MasterSession([signup, organisation, None, None])
    _patch_tenant(monkeypatch, _TenantSession())

    result = await OrganisationMembershipService(master).join_verified_identity(
        _identity("microsoft"), signup.signup_request_id
    )

    assert result.mapping is not None
    assert result.mapping.provider == ExternalIdentityProvider.AZURE_AD
    assert result.user is not None
    assert result.user.authentication_method == UserAuthenticationMethod.MICROSOFT_ENTRA


@pytest.mark.asyncio
async def test_repeated_active_join_returns_existing_user_without_side_effects() -> None:
    organisation = _organisation()
    user = User(
        organisation_id=organisation.organisation_id,
        email="teammate@acme.com",
        first_name="Team",
        last_name="Member",
        user_role=UserAppRole.MEMBER,
    )
    mapping = ExternalIdentityMapping(
        user_id=user.user_id,
        organisation_id=organisation.organisation_id,
        provider=ExternalIdentityProvider.GOOGLE,
        provider_type=ExternalIdentityProviderType.OAUTH,
        external_user_id="provider-user",
        external_tenant_id="acme.com",
    )
    signup = _signup(
        organisation,
        status=SignupRequestStatus.ACTIVE,
        user_id=user.user_id,
    )
    master = _MasterSession(
        [signup, mapping],
        rows={(User, user.user_id): user},
    )

    result = await OrganisationMembershipService(master).join_verified_identity(
        _identity(), signup.signup_request_id
    )

    assert result.user is user
    assert result.mapping is mapping
    assert result.notify_admins is False
    assert master.added == []


@pytest.mark.asyncio
async def test_master_failure_compensates_created_tenant_user(monkeypatch) -> None:
    organisation = _organisation()
    signup = _signup(organisation)
    master = _MasterSession([signup, organisation, None, None], fail_commit=True)
    tenant = _TenantSession()
    _patch_tenant(monkeypatch, tenant)

    with pytest.raises(RuntimeError, match="master commit failed"):
        await OrganisationMembershipService(master).join_verified_identity(
            _identity(), signup.signup_request_id
        )

    assert master.rollbacks == 1
    assert tenant.deleted == 1
    assert tenant.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["open", "invite_only"])
async def test_admin_can_invite_member_in_either_policy(monkeypatch, policy) -> None:
    organisation = _organisation(policy)
    organisation.domain = ["acme.com"]
    inviter = User(
        organisation_id=organisation.organisation_id,
        supabase_user_id="admin-auth-id",
        email="admin@acme.com",
        first_name="Admin",
        last_name="User",
        user_role=UserAppRole.ADMIN,
    )
    master = _MasterSession([None])
    _patch_tenant(monkeypatch, _TenantSession())

    user = await OrganisationMembershipService(master).invite_member(
        organisation=organisation,
        inviter=inviter,
        request=OrganisationMemberInvitationRequest(
            email="invitee@acme.com",
            first_name="Invitee",
            last_name="User",
            authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
        ),
    )

    assert user.user_role == UserAppRole.MEMBER
    assert user.organisation_id == organisation.organisation_id
    assert user.supabase_user_id is None
    assert user in master.added


@pytest.mark.asyncio
async def test_repeated_invitation_reuses_same_unlinked_member() -> None:
    organisation = _organisation()
    existing = User(
        organisation_id=organisation.organisation_id,
        email="invitee@acme.com",
        first_name="Invitee",
        last_name="User",
        user_role=UserAppRole.MEMBER,
        organisation_role="Member",
        authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
    )
    inviter = User(
        organisation_id=organisation.organisation_id,
        email="admin@acme.com",
        first_name="Admin",
        last_name="User",
        user_role=UserAppRole.ADMIN,
    )
    master = _MasterSession([existing])

    returned = await OrganisationMembershipService(master).invite_member(
        organisation=organisation,
        inviter=inviter,
        request=OrganisationMemberInvitationRequest(
            email="invitee@acme.com",
            first_name="Changed",
            last_name="Name",
            authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
        ),
    )

    assert returned is existing
    assert master.added == []
    assert master.commits == 0


@pytest.mark.asyncio
async def test_invitation_rejects_email_outside_organisation_domain() -> None:
    organisation = _organisation()
    inviter = User(
        organisation_id=organisation.organisation_id,
        email="admin@acme.com",
        first_name="Admin",
        last_name="User",
        user_role=UserAppRole.ADMIN,
    )

    with pytest.raises(OrganisationMembershipError) as error:
        await OrganisationMembershipService(_MasterSession()).invite_member(
            organisation=organisation,
            inviter=inviter,
            request=OrganisationMemberInvitationRequest(
                email="person@other.com",
                first_name="Other",
                last_name="Person",
                authentication_method=UserAuthenticationMethod.MICROSOFT_ENTRA,
            ),
        )

    assert error.value.code == "INVALID_COMPANY_EMAIL_DOMAIN"
