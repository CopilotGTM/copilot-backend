"""Tests for Users Auth API handler.

Focuses on:
- Login endpoint with various scenarios (success, failures, password change)
- Reset password endpoint with token validation
- Refresh token endpoint
- Forgot password endpoint (always returns 200 for security)
- Logout endpoint (single and global)
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from starlette.requests import Request
from tests.api.routers.users.conftest import create_user


class _SSOResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _SSOSession:
    def __init__(self, user, organisation, mapping=None):
        self.user = user
        self.organisation = organisation
        self.mapping = mapping
        self.added = []
        self.commits = 0
        self.email_queried = False

    async def execute(self, statement):
        query = str(statement)
        if "external_identity_mappings" in query:
            return _SSOResult(self.mapping)
        if "lower(public.users.email)" in query:
            self.email_queried = True
            return _SSOResult(self.user)
        if "supabase_user_id" in query:
            return _SSOResult(self.user if self.user and self.user.supabase_user_id else None)
        return _SSOResult(self.user)

    async def get(self, model, _identity):
        return self.user if getattr(model, "__name__", "") == "User" else self.organisation

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1
        return None


@pytest.mark.asyncio
async def test_login_metadata_failure_is_best_effort(mocker):
    import src.api.routers.users.auth_handler as auth_handler

    class FailingSession:
        async def execute(self, _statement):
            raise RuntimeError("database unavailable")

    @asynccontextmanager
    async def session_scope():
        yield FailingSession()

    mocker.patch.object(auth_handler, "master_async_session_scope", session_scope)

    recorded_at = await auth_handler._record_successful_login(uuid4(), "Asia/Kolkata")

    assert recorded_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_google_sso_login_links_an_invited_approved_user(mocker):
    """A verified Workspace identity links only after app-side access checks."""
    import src.api.routers.users.auth_handler as auth_handler
    from src.domain.models.master_tables import (
        AdminUserStatus,
        OrganisationStatus,
        SupabaseUserStatus,
        UserAuthenticationMethod,
    )
    from src.infra.auth.supabase_user_client import VerifiedSupabaseSession

    user = SimpleNamespace(
        user_id=uuid4(),
        email="person@acme.com",
        first_name="Person",
        last_name="Acme",
        user_role="member",
        created_at=datetime.now(UTC),
        preferences={},
        organisation_id=uuid4(),
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
        supabase_user_id=None,
        email_verified=False,
        last_login_at=None,
        timezone=None,
        profile_image_url=None,
        authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
    )
    organisation = SimpleNamespace(
        organisation_id=user.organisation_id,
        domain=["acme.com"],
        is_active=True,
        status=OrganisationStatus.APPROVED,
        preferences={},
    )
    session = _SSOSession(user, organisation)

    @asynccontextmanager
    async def session_scope():
        yield session

    mocker.patch.object(auth_handler, "master_async_session_scope", session_scope)
    mocker.patch.object(
        auth_handler.supabase_user_client,
        "verify_session",
        return_value=VerifiedSupabaseSession(
            user={
                "id": "supabase-google-user",
                "email": user.email,
                "email_confirmed_at": "2026-08-01T00:00:00Z",
                "identities": [
                    {
                        "provider": "google",
                        "identity_data": {
                            "sub": "google-subject-1",
                            "email": user.email,
                            "email_verified": True,
                            "hd": "acme.com",
                            "picture": "https://lh3.googleusercontent.com/avatar.png",
                        },
                    }
                ],
            },
            claims={"sub": "supabase-google-user"},
            supabase_user_id="supabase-google-user",
            authentication_method=UserAuthenticationMethod.GOOGLE_WORKSPACE,
        ),
    )

    result = await auth_handler.user_external_auth_complete(
        provider="google",
        request=Request({"type": "http", "headers": [(b"authorization", b"Bearer token")]}),
        response=FastAPIResponse(),
        _rl_login=None,
    )

    assert result.data is not None
    assert result.data.email == user.email
    assert user.supabase_user_id == "supabase-google-user"
    assert user.profile_image_url == "https://lh3.googleusercontent.com/avatar.png"


@pytest.mark.asyncio
async def test_returning_external_user_resolves_connection_before_changed_email(mocker):
    import src.api.routers.users.auth_handler as auth_handler
    from src.domain.models.master_tables import (
        AdminUserStatus,
        OrganisationStatus,
        SupabaseUserStatus,
        UserAuthenticationMethod,
    )
    from src.infra.auth.supabase_user_client import VerifiedSupabaseSession

    user = SimpleNamespace(
        user_id=uuid4(),
        email="old-name@acme.com",
        first_name="Person",
        last_name="Acme",
        user_role="member",
        created_at=datetime.now(UTC),
        preferences={},
        organisation_id=uuid4(),
        status=SupabaseUserStatus.ACTIVE,
        admin_status=AdminUserStatus.APPROVED,
        supabase_user_id="supabase-microsoft-user",
        email_verified=True,
        last_login_at=None,
        timezone=None,
        profile_image_url=None,
        authentication_method=UserAuthenticationMethod.MICROSOFT_ENTRA,
    )
    organisation = SimpleNamespace(
        organisation_id=user.organisation_id,
        domain=["acme.com"],
        is_active=True,
        status=OrganisationStatus.APPROVED,
        preferences={},
    )
    tenant_id = str(uuid4())
    mapping = SimpleNamespace(
        user_id=user.user_id,
        is_active=True,
        last_used_at=None,
    )
    session = _SSOSession(user, organisation, mapping)

    @asynccontextmanager
    async def session_scope():
        yield session

    mocker.patch.object(auth_handler, "master_async_session_scope", session_scope)
    mocker.patch.object(
        auth_handler.supabase_user_client,
        "verify_session",
        return_value=VerifiedSupabaseSession(
            user={
                "id": "supabase-microsoft-user",
                "email": "new-name@acme.com",
                "email_confirmed_at": "2026-08-01T00:00:00Z",
                "identities": [
                    {
                        "provider": "azure",
                        "identity_data": {
                            "sub": "entra-subject",
                            "tid": tenant_id,
                            "email": "new-name@acme.com",
                            "email_verified": True,
                        },
                    }
                ],
            },
            claims={"sub": "supabase-microsoft-user"},
            supabase_user_id="supabase-microsoft-user",
            authentication_method=UserAuthenticationMethod.MICROSOFT_ENTRA,
        ),
    )

    result = await auth_handler.user_external_auth_complete(
        provider="microsoft",
        request=Request({"type": "http", "headers": [(b"authorization", b"Bearer token")]}),
        response=FastAPIResponse(),
        _rl_login=None,
    )

    assert result.data is not None
    assert result.data.email == "old-name@acme.com"
    assert session.email_queried is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "authentication_method", "identity_provider", "identity_data"),
    [
        (
            "google",
            "google_workspace",
            "google",
            {
                "sub": "google-unknown-user",
                "email": "new-user@acme.com",
                "email_verified": True,
                "hd": "acme.com",
            },
        ),
        (
            "microsoft",
            "microsoft_entra",
            "azure",
            {
                "sub": "microsoft-unknown-user",
                "tid": "00000000-0000-4000-8000-000000000001",
                "email": "new-user@acme.com",
                "email_verified": True,
            },
        ),
    ],
)
async def test_unknown_external_user_is_directed_to_signup_without_writes(
    mocker,
    provider,
    authentication_method,
    identity_provider,
    identity_data,
):
    import src.api.routers.users.auth_handler as auth_handler
    from src.domain.models.master_tables import UserAuthenticationMethod
    from src.infra.auth.supabase_user_client import VerifiedSupabaseSession

    session = _SSOSession(user=None, organisation=None)

    @asynccontextmanager
    async def session_scope():
        yield session

    mocker.patch.object(auth_handler, "master_async_session_scope", session_scope)
    mocker.patch.object(
        auth_handler.supabase_user_client,
        "verify_session",
        return_value=VerifiedSupabaseSession(
            user={
                "id": f"supabase-{provider}-unknown-user",
                "email": "new-user@acme.com",
                "email_confirmed_at": "2026-08-01T00:00:00Z",
                "identities": [{"provider": identity_provider, "identity_data": identity_data}],
            },
            claims={"sub": f"supabase-{provider}-unknown-user"},
            supabase_user_id=f"supabase-{provider}-unknown-user",
            authentication_method=UserAuthenticationMethod(authentication_method),
        ),
    )
    response = FastAPIResponse()

    result = await auth_handler.user_external_auth_complete(
        provider=provider,
        request=Request({"type": "http", "headers": [(b"authorization", b"Bearer token")]}),
        response=response,
        _rl_login=None,
    )

    assert response.status_code == 404
    assert result.code == "ACCOUNT_NOT_FOUND"
    assert result.message == "We couldn't find a CopilotGTM account for this email."
    assert result.data is None
    assert session.email_queried is True
    assert session.added == []
    assert session.commits == 0


# =============================================================================
# POST /users/auth/login - Login Tests
# =============================================================================


def test_login_success(client_with_user_auth):
    """Test successful login returns tokens and user data."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "correctpassword"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["email"] == "testuser@example.com"
    assert body["data"]["state"] == "ACTIVE"
    assert body["data"]["tokens"] is not None
    assert "access_token" in body["data"]["tokens"]
    assert "refresh_token" in body["data"]["tokens"]


def test_unknown_password_user_has_generic_stable_error(client_with_user_auth):
    response = client_with_user_auth["client"].post(
        "/users/auth/login",
        json={"email": "missing@example.com", "password": "invalid-value"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"
    assert response.json()["message"] == "Invalid email or password"


def test_provider_account_password_login_returns_provider_guidance(client_with_user_auth):
    from src.domain.models.master_tables import UserAuthenticationMethod

    client_with_user_auth["user"].authentication_method = UserAuthenticationMethod.MICROSOFT_ENTRA

    response = client_with_user_auth["client"].post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "correctpassword"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "MICROSOFT_SIGN_IN_REQUIRED"


def test_me_returns_google_profile_image(client_with_user_auth):
    """The identity image stored at SSO link time is available to the app shell."""
    client = client_with_user_auth["client"]
    user = client_with_user_auth["user"]
    user.profile_image_url = "https://example.com/avatar.png"

    response = client.get("/users/auth/me", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["profile_image_url"] == user.profile_image_url


@pytest.mark.asyncio
async def test_login_success_omits_migrated_columns_but_preserves_other_preferences(
    user_auth_test_data, mocker
):
    """Column catalogs have a dedicated endpoint; login keeps unrelated preferences."""
    import src.api.routers.users.auth_handler as auth_handler
    from src.domain.models.user_models import UserLoginRequest

    @asynccontextmanager
    async def fake_master_async_session_scope():
        """Yield the fake master session for the direct handler test."""
        yield user_auth_test_data["master_session"]

    mocker.patch.object(
        auth_handler,
        "master_async_session_scope",
        fake_master_async_session_scope,
    )
    mocker.patch.object(
        auth_handler,
        "supabase_user_client",
        user_auth_test_data["supabase_client"],
    )
    mocker.patch.object(auth_handler, "_load_organisation_preferences", return_value={})
    result = await auth_handler.user_login(
        request=Request({"type": "http", "headers": []}),
        login_request=UserLoginRequest(
            email="testuser@example.com",
            password="correctpassword",
        ),
        response=FastAPIResponse(),
        _rl_login=None,
    )

    assert result.data is not None
    assert result.data.user_preferences is not None
    assert "columns" not in result.data.user_preferences["crm"]["contacts"]["list"]
    assert "columns" not in result.data.user_preferences["crm"]["accounts"]["list"]
    assert "columns" not in result.data.user_preferences["crm"]["deals"]["list"]
    assert "columns" not in result.data.user_preferences["crm"]["tasks"]["list"]
    assert "columns" not in result.data.user_preferences["crm"]["leads"]["list"]
    assert result.data.user_preferences["navigation"]["last_settings_item"] == "integrations"
    assert (
        result.data.user_preferences["navigation"]["expanded_sections"]["settings_crm_fields"]
        is False
    )


def test_login_success_updates_last_login_at(client_with_user_auth):
    """Test successful active login persists a fresh last_login_at timestamp."""
    client = client_with_user_auth["client"]
    user = client_with_user_auth["user"]
    previous_last_login_at = user.last_login_at

    resp = client.post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "correctpassword"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["last_login_at"] is not None
    assert user.last_login_at is not None
    assert user.last_login_at > previous_last_login_at


def test_login_uses_timezone_header_and_persists_profile(client_with_user_auth):
    """Test login captures the preferred user timezone from the request header."""
    client = client_with_user_auth["client"]
    user = client_with_user_auth["user"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "correctpassword"},
        headers={"X-User-Timezone": "Asia/Kolkata"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["timezone"] == "Asia/Kolkata"
    assert user.timezone == "Asia/Kolkata"


def test_login_ignores_malformed_timezone_header(client_with_user_auth):
    """Test malformed timezone headers are ignored instead of crashing the request."""
    client = client_with_user_auth["client"]
    user = client_with_user_auth["user"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "correctpassword"},
        headers={"X-User-Timezone": "../../etc/passwd"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["timezone"] is None
    assert user.timezone is None


def test_login_user_not_found(client_with_user_auth):
    """Test login with non-existent email returns unauthorized."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "nonexistent@example.com", "password": "password123"},
    )
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert body["message"] == "Invalid email or password"
    assert body["code"] == "INVALID_CREDENTIALS"


def test_login_invalid_password(client_with_user_auth):
    """Test login with wrong password returns unauthorized."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert body["message"] == "Invalid email or password"
    assert body["code"] == "INVALID_CREDENTIALS"


def test_suspended_password_user_is_rejected_after_valid_credentials(
    client_with_user_auth,
):
    from src.domain.models.master_tables import AdminUserStatus

    client_with_user_auth["user"].admin_status = AdminUserStatus.SUSPENDED

    response = client_with_user_auth["client"].post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "correctpassword"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ACCOUNT_INACTIVE"


def test_login_password_change_required(client_with_user_auth):
    """Test login requiring password change returns PENDING_SETUP state."""
    client = client_with_user_auth["client"]
    supabase_client = client_with_user_auth["supabase_client"]
    master_session = client_with_user_auth["master_session"]

    # Add user requiring password change
    user, supabase_id = create_user(
        email="newuser@example.com",
        password="temppassword",
        requires_password_change=True,
    )
    master_session.add_user(user)
    supabase_client.add_user(
        email="newuser@example.com",
        password="temppassword",
        user_id=supabase_id,
        requires_password_change=True,
    )

    resp = client.post(
        "/users/auth/login",
        json={"email": "newuser@example.com", "password": "temppassword"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["state"] == "PASSWORD_CHANGE_REQUIRED"
    assert body["data"]["tokens"] is not None
    assert "password_reset_link" not in body["data"]


def test_login_password_change_required_updates_last_login_at(client_with_user_auth):
    """Test successful pending-setup login still records the most recent login time."""
    client = client_with_user_auth["client"]
    supabase_client = client_with_user_auth["supabase_client"]
    master_session = client_with_user_auth["master_session"]

    user, supabase_id = create_user(
        email="newuser@example.com",
        password="temppassword",
        requires_password_change=True,
    )
    previous_last_login_at = user.last_login_at
    master_session.add_user(user)
    supabase_client.add_user(
        email="newuser@example.com",
        password="temppassword",
        user_id=supabase_id,
        requires_password_change=True,
    )

    resp = client.post(
        "/users/auth/login",
        json={"email": "newuser@example.com", "password": "temppassword"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["state"] == "PASSWORD_CHANGE_REQUIRED"
    assert user.last_login_at is not None
    assert user.last_login_at > previous_last_login_at


def test_login_password_change_required_persists_timezone_header(client_with_user_auth):
    """Test PENDING_SETUP login still captures and returns the user's timezone."""
    client = client_with_user_auth["client"]
    supabase_client = client_with_user_auth["supabase_client"]
    master_session = client_with_user_auth["master_session"]

    user, supabase_id = create_user(
        email="setupuser@example.com",
        password="temppassword",
        requires_password_change=True,
    )
    master_session.add_user(user)
    supabase_client.add_user(
        email="setupuser@example.com",
        password="temppassword",
        user_id=supabase_id,
        requires_password_change=True,
    )

    resp = client.post(
        "/users/auth/login",
        json={"email": "setupuser@example.com", "password": "temppassword"},
        headers={"X-User-Timezone": "America/New_York"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["state"] == "PASSWORD_CHANGE_REQUIRED"
    assert body["data"]["timezone"] == "America/New_York"
    assert user.timezone == "America/New_York"


def test_complete_password_setup_uses_authenticated_temporary_session(
    client_with_user_auth,
):
    client = client_with_user_auth["client"]
    supabase_client = client_with_user_auth["supabase_client"]
    master_session = client_with_user_auth["master_session"]
    from src.domain.models.master_tables import SupabaseUserStatus

    user, supabase_id = create_user(
        email="setup@example.com",
        password="temporary-password",
        requires_password_change=True,
        status=SupabaseUserStatus.PENDING_SETUP,
    )
    master_session.add_user(user)
    supabase_client.add_user(
        email="setup@example.com",
        password="temporary-password",
        user_id=supabase_id,
        requires_password_change=True,
    )
    login = client.post(
        "/users/auth/login",
        json={"email": "setup@example.com", "password": "temporary-password"},
    ).json()
    access_token = login["data"]["tokens"]["access_token"]

    response = client.post(
        "/users/auth/complete-password-setup",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"new_password": "permanent-password", "confirm_password": "permanent-password"},
    )

    assert response.status_code == 200, response.text
    assert (supabase_id, "permanent-password") in supabase_client._updated_passwords
    assert (access_token, False) in supabase_client._revoked_access_tokens
    assert user.status == SupabaseUserStatus.ACTIVE


def test_login_invalid_email_format(client_with_user_auth):
    """Test login with invalid email format returns 422."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "not-an-email", "password": "password123"},
    )
    assert resp.status_code == 422, resp.text


def test_login_missing_password(client_with_user_auth):
    """Test login without password returns 422."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# POST /users/auth/reset-password - Reset Password Tests
# =============================================================================


def test_reset_password_success(client_with_user_auth):
    """Test successful password reset with valid token."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "newpassword123",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "Password changed successfully" in body["message"]


def test_reset_password_missing_token(client_with_user_auth):
    """Test reset password without token returns bad request."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "",
            "new_password": "newpassword123",
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "Recovery token" in body["message"] or "token_hash" in body["message"]


def test_reset_password_missing_password(client_with_user_auth):
    """Test reset password without new password returns bad request."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "",
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "New password" in body["message"] or "password is required" in body["message"]


def test_reset_password_mismatch(client_with_user_auth):
    """Test reset password with mismatched confirmation returns bad request."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "newpassword123",
            "confirm_password": "differentpassword",
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "do not match" in body["message"] or "mismatch" in body["message"].lower()


def test_reset_password_invalid_token(client_with_user_auth):
    """Test reset password with invalid token returns bad request."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "invalid_token",
            "new_password": "newpassword123",
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "Invalid" in body["message"] or "expired" in body["message"]


# =============================================================================
# POST /users/auth/refresh-token - Refresh Token Tests
# =============================================================================


def test_refresh_token_success(client_with_user_auth):
    """Test successful token refresh returns new tokens."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/refresh-token",
        json={"refresh_token": "valid_refresh_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["access_token"] is not None
    assert body["data"]["refresh_token"] is not None


def test_refresh_token_invalid(client_with_user_auth):
    """Test refresh with invalid token returns unauthorized."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/refresh-token",
        json={"refresh_token": "invalid_refresh_token"},
    )
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "Invalid" in body["message"] or "invalid" in body["message"].lower()


def test_refresh_token_missing(client_with_user_auth):
    """Test refresh without token returns 422."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/refresh-token",
        json={},
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# POST /users/auth/forgot-password - Forgot Password Tests
# =============================================================================


def test_forgot_password_success(client_with_user_auth):
    """Test forgot password sends email and returns 200."""
    client = client_with_user_auth["client"]
    email_service = client_with_user_auth["email_service"]

    resp = client.post(
        "/users/auth/forgot-password",
        json={"email": "testuser@example.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "If an account with this email exists" in body["message"]

    # Verify email was sent
    assert len(email_service._sent_emails) == 1
    assert email_service._sent_emails[0]["to_email"] == "testuser@example.com"


def test_forgot_password_user_not_found(client_with_user_auth):
    """Test forgot password for non-existent user still returns 200 (security).

    This prevents user enumeration attacks.
    """
    client = client_with_user_auth["client"]
    email_service = client_with_user_auth["email_service"]

    resp = client.post(
        "/users/auth/forgot-password",
        json={"email": "nonexistent@example.com"},
    )
    # Should return 200 to prevent user enumeration
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "If an account with this email exists" in body["message"]

    # No email should be sent
    assert len(email_service._sent_emails) == 0


def test_forgot_password_invalid_email(client_with_user_auth):
    """Test forgot password with invalid email format returns 422."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/forgot-password",
        json={"email": "not-an-email"},
    )
    assert resp.status_code == 422, resp.text


def test_suspended_user_cannot_refresh_session(client_with_user_auth):
    from src.domain.models.master_tables import AdminUserStatus

    client_with_user_auth["user"].admin_status = AdminUserStatus.SUSPENDED

    response = client_with_user_auth["client"].post(
        "/users/auth/refresh-token",
        json={"refresh_token": "valid_refresh_token"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ACCOUNT_INACTIVE"


def test_provider_account_forgot_password_is_suppressed(client_with_user_auth):
    from src.domain.models.master_tables import UserAuthenticationMethod

    client_with_user_auth["user"].authentication_method = UserAuthenticationMethod.GOOGLE_WORKSPACE

    response = client_with_user_auth["client"].post(
        "/users/auth/forgot-password",
        json={"email": "testuser@example.com"},
    )

    assert response.status_code == 200
    assert client_with_user_auth["supabase_client"]._generated_reset_links == []
    assert client_with_user_auth["email_service"]._sent_emails == []


def test_provider_account_recovery_token_cannot_set_password(client_with_user_auth):
    from src.domain.models.master_tables import UserAuthenticationMethod

    client_with_user_auth["user"].authentication_method = UserAuthenticationMethod.MICROSOFT_ENTRA

    response = client_with_user_auth["client"].post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "newpassword123",
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_RECOVERY_TOKEN"
    assert client_with_user_auth["supabase_client"]._updated_passwords == []


# =============================================================================
# POST /users/auth/logout - Logout Tests
# =============================================================================


def test_logout_success(client_with_user_auth):
    """Test successful logout from current session."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/logout",
        json={"global_logout": False},
        headers={"Authorization": "Bearer logout-token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "Successfully logged out" in body["message"]
    assert "all sessions" not in body["message"]
    assert ("logout-token", False) in client_with_user_auth[
        "supabase_client"
    ]._revoked_access_tokens


def test_logout_global(client_with_user_auth):
    """Test global logout from all sessions."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/logout",
        json={"global_logout": True},
        headers={"Authorization": "Bearer logout-token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "all sessions" in body["message"]
    assert ("logout-token", True) in client_with_user_auth["supabase_client"]._revoked_access_tokens


def test_logout_default_not_global(client_with_user_auth):
    """Test logout defaults to single session."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/logout",
        json={},
        headers={"Authorization": "Bearer logout-token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "all sessions" not in body["message"]
