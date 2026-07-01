"""Tests for Users Auth API handler.

Focuses on:
- Login endpoint with various scenarios (success, failures, password change)
- Reset password endpoint with token validation
- Refresh token endpoint
- Forgot password endpoint (always returns 200 for security)
- Logout endpoint (single and global)
"""

from contextlib import asynccontextmanager

import pytest
from fastapi import Response as FastAPIResponse
from starlette.requests import Request
from tests.api.routers.users.conftest import create_user

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


@pytest.mark.asyncio
async def test_login_success_returns_default_contact_list_columns(user_auth_test_data, mocker):
    """Test login bootstraps ordered contact list columns for the frontend."""
    import src.api.routers.users.auth_handler as auth_handler
    from src.domain.models.user_models import UserLoginRequest

    @asynccontextmanager
    async def fake_master_async_session_scope():
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
    assert result.data.user_preferences["crm"]["contacts"]["list"]["columns"] == [
        "full_name",
        "job_title",
        "company.name",
        "email",
        "phone",
        "linkedin_url",
        "owner.name",
        "deal_count",
        "last_contacted_at",
        "last_activity_at",
    ]
    assert result.data.user_preferences["crm"]["deals"]["list"]["columns"] == [
        "name",
        "company.name",
        "stage.name",
        "amount",
        "expected_close_date",
        "owner.name",
        "primary_contact.name",
        "last_activity_at",
        "deal_status",
        "pipeline.name",
    ]
    assert result.data.user_preferences["crm"]["tasks"]["list"]["columns"] == [
        "subject",
        "due_date",
        "assigned_to.name",
        "deal.name",
        "priority",
        "status",
        "has_reminder",
    ]


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
    """Test login with non-existent email returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "nonexistent@example.com", "password": "password123"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "User not found" in body["message"]


def test_login_invalid_password(client_with_user_auth):
    """Test login with wrong password returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/login",
        json={"email": "testuser@example.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "Invalid credentials" in body["message"]


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
    assert body["data"]["password_reset_link"] is not None


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
            "action": "forgot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "Password changed successfully" in body["message"]


def test_reset_password_missing_token(client_with_user_auth):
    """Test reset password without token returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "",
            "new_password": "newpassword123",
            "action": "forgot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "Recovery token" in body["message"] or "token_hash" in body["message"]


def test_reset_password_missing_password(client_with_user_auth):
    """Test reset password without new password returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "",
            "action": "forgot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "New password" in body["message"] or "password is required" in body["message"]


def test_reset_password_mismatch(client_with_user_auth):
    """Test reset password with mismatched confirm returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "newpassword123",
            "confirm_password": "differentpassword",
            "action": "forgot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "do not match" in body["message"] or "mismatch" in body["message"].lower()


def test_reset_password_invalid_token(client_with_user_auth):
    """Test reset password with invalid token returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "invalid_token",
            "new_password": "newpassword123",
            "action": "forgot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "Invalid" in body["message"] or "expired" in body["message"]


def test_reset_password_invalid_action(client_with_user_auth):
    """Test reset password with invalid action returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "newpassword123",
            "action": "invalid_action",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "Invalid action" in body["message"] or "force_change" in body["message"]


def test_reset_password_force_change_success(client_with_user_auth):
    """Test force_change action updates user metadata."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "newpassword123",
            "action": "force_change",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


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
    """Test refresh with invalid token returns error status.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/refresh-token",
        json={"refresh_token": "invalid_refresh_token"},
    )
    assert resp.status_code == 200, resp.text
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


# =============================================================================
# POST /users/auth/logout - Logout Tests
# =============================================================================


def test_logout_success(client_with_user_auth):
    """Test successful logout from current session."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/logout",
        json={"global_logout": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "Successfully logged out" in body["message"]
    assert "all sessions" not in body["message"]


def test_logout_global(client_with_user_auth):
    """Test global logout from all sessions."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/logout",
        json={"global_logout": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "all sessions" in body["message"]


def test_logout_default_not_global(client_with_user_auth):
    """Test logout defaults to single session."""
    client = client_with_user_auth["client"]

    resp = client.post(
        "/users/auth/logout",
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "all sessions" not in body["message"]
