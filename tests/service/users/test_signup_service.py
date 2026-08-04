"""Service-level contracts for resumable signup and founder provisioning.

The in-memory session exposes query ordering and committed side effects without
requiring PostgreSQL; route and migration tests cover the surrounding boundaries.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy.sql.elements import TextClause
from src.domain.models.master_tables import (
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
)
from src.domain.models.user_signup_models import ExternalSignupRequest
from src.service.users.external_auth_service import (
    ExternalAuthClaims,
    ExternalSignupIdentity,
)
from src.service.users.signup_service import SignupService, SignupServiceError


class _Scalars:
    def __init__(self, value):
        self.value = value

    def first(self):
        """Return the single configured row for one service lookup."""
        return self.value


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalars(self):
        """Match SQLAlchemy's scalar-result adapter used by the service."""
        return _Scalars(self.value)


class _Session:
    def __init__(self, query_results=(), rows=None):
        self.query_results = list(query_results)
        self.rows = rows or {}
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, statement, _parameters=None):
        """Consume one configured query result while accepting advisory locks."""
        if isinstance(statement, TextClause):
            return _Result()
        return _Result(self.query_results.pop(0))

    def add(self, value):
        """Record pending models so tests can inspect transaction contents."""
        self.added.append(value)

    async def commit(self):
        """Count durable boundaries without opening a database transaction."""
        self.commits += 1

    async def flush(self):
        """Count flushes without opening a database transaction."""
        self.flushes += 1

    async def refresh(self, _value):
        """Preserve the async-session contract after an in-memory commit."""
        return None

    async def get(self, model, identifier):
        """Resolve preconfigured rows used by control-plane retry tests."""
        return self.rows.get((model, identifier))


def _identity(**overrides) -> ExternalSignupIdentity:
    claims = ExternalAuthClaims(
        authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
        mapping_provider=ExternalIdentityProvider.GOOGLE,
        supabase_user_id="supabase-user",
        provider_user_id="provider-user",
        provider_tenant_id="acme.com",
        email="founder@acme.com",
        full_name="Founding Admin",
        profile_image_url=None,
    )
    values = {**claims.__dict__, **overrides}
    return ExternalSignupIdentity(
        claims=ExternalAuthClaims(**values),
        primary_domain=values["email"].rsplit("@", 1)[-1],
    )


def _request(**overrides) -> ExternalSignupRequest:
    values = {"organisation_name": "Acme"}
    values.update(overrides)
    return ExternalSignupRequest.model_validate(values)


def _signup(**overrides) -> SignupRequest:
    values = {
        "idempotency_key_hash": "a" * 64,
        "request_fingerprint": "b" * 64,
        "provider": ExternalSignupProvider.GOOGLE_WORKSPACE,
        "supabase_user_id": "supabase-user",
        "provider_user_id": "provider-user",
        "provider_tenant_id": "acme.com",
        "normalized_email": "founder@acme.com",
        "first_name": "Founding",
        "last_name": "Admin",
        "organisation_name": None,
        "primary_domain": "acme.com",
        "country": None,
        "terms_version": None,
        "privacy_version": None,
        "profile_image_url": None,
        "profile_incomplete": False,
        "status": SignupRequestStatus.DRAFT,
    }
    values.update(overrides)
    return SignupRequest(**values)


@pytest.mark.asyncio
async def test_new_signup_persists_provider_identity_as_draft() -> None:
    """Preflight persists provider identity without reserving a workspace domain."""
    session = _Session(query_results=[None, None, None])

    signup = await SignupService(session).get_or_create_draft(
        identity=_identity(),
    )

    assert signup in session.added
    assert signup.normalized_email == "founder@acme.com"
    assert signup.provider == ExternalSignupProvider.GOOGLE_WORKSPACE
    assert signup.status == SignupRequestStatus.DRAFT
    assert signup.organisation_name is None
    assert session.commits == 1


@pytest.mark.asyncio
async def test_missing_provider_name_uses_non_authoritative_email_fallback() -> None:
    """A missing provider name defers profile completion instead of adding a form."""
    session = _Session(query_results=[None, None, None])

    signup = await SignupService(session).get_or_create_draft(
        identity=_identity(full_name=None),
    )

    assert signup.first_name == "founder"
    assert signup.last_name == ""
    assert signup.profile_incomplete is True


@pytest.mark.asyncio
async def test_existing_draft_is_resumed_without_another_write() -> None:
    """Returning after a closed tab reuses the same durable draft."""
    existing = _signup()
    session = _Session(query_results=[existing])

    returned = await SignupService(session).get_or_create_draft(identity=_identity())

    assert returned is existing
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_finalize_draft_records_only_workspace_input_and_server_policy() -> None:
    """Create workspace stores the one user input and server-owned policy versions."""
    draft = _signup()
    session = _Session(query_results=[draft, None, None, None, None])

    signup = await SignupService(session).finalize_signup(
        identity=_identity(),
        request=_request(),
    )

    assert signup is draft
    assert signup.organisation_name == "Acme"
    assert signup.country is None
    assert signup.terms_version
    assert signup.privacy_version
    assert signup.status == SignupRequestStatus.RECEIVED
    assert session.commits == 1


@pytest.mark.asyncio
async def test_finalize_refreshes_provider_profile_from_verified_session() -> None:
    """A resumed draft cannot create the founder from stale provider profile data."""
    draft = _signup(
        normalized_email="old@acme.com",
        first_name="Old",
        last_name="Name",
        profile_incomplete=True,
    )
    session = _Session(query_results=[draft, None, None, None, None])

    signup = await SignupService(session).finalize_signup(
        identity=_identity(profile_image_url="https://lh3.googleusercontent.com/avatar.png"),
        request=_request(),
    )

    assert signup.normalized_email == "founder@acme.com"
    assert signup.first_name == "Founding"
    assert signup.last_name == "Admin"
    assert signup.profile_image_url == "https://lh3.googleusercontent.com/avatar.png"
    assert signup.profile_incomplete is False


@pytest.mark.asyncio
async def test_repeated_finalize_does_not_dispatch_again() -> None:
    """A repeated Create workspace action cannot enqueue duplicate provisioning."""
    received = _signup(status=SignupRequestStatus.RECEIVED, organisation_name="Acme")
    session = _Session(query_results=[received])

    signup = await SignupService(session).finalize_signup(
        identity=_identity(),
        request=_request(),
    )

    assert signup is received
    assert session.commits == 0


@pytest.mark.asyncio
async def test_existing_user_cannot_create_a_second_organisation() -> None:
    """A founder already bound to an app user cannot create another organisation."""
    existing_user = SimpleNamespace(supabase_user_id="supabase-user")
    session = _Session(query_results=[_signup(), None, existing_user])

    with pytest.raises(SignupServiceError) as error:
        await SignupService(session).finalize_signup(
            identity=_identity(),
            request=_request(),
        )

    assert error.value.code == "ACCOUNT_IN_OTHER_ORGANISATION"


@pytest.mark.asyncio
async def test_control_plane_accepts_database_hydrated_provider_string() -> None:
    """A submitted signup survives VARCHAR hydration before founder creation."""
    signup = _signup(
        provider=ExternalSignupProvider.GOOGLE_WORKSPACE.value,
        status=SignupRequestStatus.RECEIVED.value,
        organisation_name="Acme",
        profile_image_url="https://lh3.googleusercontent.com/avatar.png",
    )
    session = _Session(query_results=[signup])

    returned, organisation, user, founder = await SignupService(session).ensure_control_plane(
        signup.signup_request_id
    )

    mapping = next(value for value in session.added if isinstance(value, ExternalIdentityMapping))
    assert returned is signup
    assert organisation.status == OrganisationStatus.PROVISIONING
    assert organisation.primary_domain == "acme.com"
    assert user.user_role == UserAppRole.ADMIN
    assert user.status == SupabaseUserStatus.ACTIVE
    assert user.profile_image_url == "https://lh3.googleusercontent.com/avatar.png"
    assert mapping.user_id == user.user_id
    assert founder.user_id == user.user_id
    assert signup.status == SignupRequestStatus.CONTROL_PLANE_CREATED
    assert session.flushes == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_control_plane_retry_reuses_existing_organisation_and_user() -> None:
    """Worker retries reuse committed control-plane identities without duplicate writes."""
    organisation = Organisation(organisation_name="Acme", status=OrganisationStatus.PROVISIONING)
    user = User(
        organisation_id=organisation.organisation_id,
        supabase_user_id="supabase-user",
        email="founder@acme.com",
        first_name="Founding",
        last_name="Admin",
        user_role=UserAppRole.ADMIN,
        organisation_role="Administrator",
        status=SupabaseUserStatus.ACTIVE,
    )
    signup = _signup(
        status=SignupRequestStatus.CONTROL_PLANE_CREATED,
        organisation_id=organisation.organisation_id,
        user_id=user.user_id,
    )
    session = _Session(
        query_results=[signup],
        rows={
            (Organisation, organisation.organisation_id): organisation,
            (User, user.user_id): user,
        },
    )

    returned, returned_org, returned_user, _ = await SignupService(session).ensure_control_plane(
        signup.signup_request_id
    )

    assert returned is signup
    assert returned_org is organisation
    assert returned_user is user
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_control_plane_rejects_unsubmitted_draft() -> None:
    """Background provisioning cannot cross the user's Create workspace boundary."""
    draft = _signup()
    session = _Session(query_results=[draft])

    with pytest.raises(SignupServiceError) as error:
        await SignupService(session).ensure_control_plane(draft.signup_request_id)

    assert error.value.code == "SIGNUP_STATE_INVALID"
