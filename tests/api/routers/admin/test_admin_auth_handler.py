"""Tests for Admin Auth API handler.

Focuses on:
- Login endpoint with various scenarios
- Reset password endpoint
- Refresh token endpoint
- Forgot password endpoint
- Logout endpoint
- Change password endpoint
"""


# =============================================================================
# POST /admin/auth/login - Login Tests
# =============================================================================


def test_login_success(admin_auth_client):
    """Test successful admin login returns tokens."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/login",
        json={"email": "admin@example.com", "password": "correctpassword"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    # Tokens are nested in the response
    assert "tokens" in body["data"]
    assert "access_token" in body["data"]["tokens"]
    assert "refresh_token" in body["data"]["tokens"]


def test_login_invalid_credentials(admin_auth_client):
    """Test login with wrong password returns error."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/login",
        json={"email": "admin@example.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_login_user_not_found(admin_auth_client):
    """Test login with non-existent email returns error."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/login",
        json={"email": "nonexistent@example.com", "password": "password123"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_login_invalid_email_format(admin_auth_client):
    """Test login with invalid email format returns 422."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/login",
        json={"email": "not-an-email", "password": "password123"},
    )
    assert resp.status_code == 422, resp.text


def test_login_missing_password(admin_auth_client):
    """Test login without password returns 422."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/login",
        json={"email": "admin@example.com"},
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# POST /admin/auth/reset-password - Reset Password Tests
# =============================================================================


def test_reset_password_success(admin_auth_client):
    """Test successful password reset with valid token."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/reset-password",
        json={
            "token_hash": "valid_recovery_token",
            "new_password": "newpassword123",
            "confirm_password": "newpassword123",
            "action": "forgot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "Password changed successfully" in body["message"]


def test_reset_password_mismatch(admin_auth_client):
    """Test reset password with mismatched passwords returns error."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/reset-password",
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


def test_reset_password_invalid_token(admin_auth_client):
    """Test reset password with invalid token returns error."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/reset-password",
        json={
            "token_hash": "invalid_token",
            "new_password": "newpassword123",
            "confirm_password": "newpassword123",
            "action": "forgot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# =============================================================================
# POST /admin/auth/refresh-token - Refresh Token Tests
# =============================================================================


def test_refresh_token_success(admin_auth_client):
    """Test successful token refresh."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/refresh-token",
        json={"refresh_token": "valid_refresh_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "access_token" in body["data"]
    assert "refresh_token" in body["data"]


def test_refresh_token_invalid(admin_auth_client):
    """Test refresh with invalid token returns error."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/refresh-token",
        json={"refresh_token": "invalid_refresh_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_refresh_token_missing(admin_auth_client):
    """Test refresh without token returns 422."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/refresh-token",
        json={},
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# POST /admin/auth/forgot-password - Forgot Password Tests
# =============================================================================


def test_forgot_password_success(admin_auth_client):
    """Test forgot password always returns 200 (security)."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/forgot-password",
        json={"email": "admin@example.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "If an account" in body["message"]


def test_forgot_password_nonexistent(admin_auth_client):
    """Test forgot password for non-existent email still returns 200 (security)."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/forgot-password",
        json={"email": "nonexistent@example.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_forgot_password_invalid_email(admin_auth_client):
    """Test forgot password with invalid email format returns 422."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/forgot-password",
        json={"email": "not-an-email"},
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# POST /admin/auth/logout - Logout Tests
# =============================================================================


def test_logout_success(admin_auth_client):
    """Test successful logout."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/logout",
        json={"global_logout": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "logged out" in body["message"].lower()


def test_logout_global(admin_auth_client):
    """Test global logout from all sessions."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/logout",
        json={"global_logout": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "all sessions" in body["message"]


# =============================================================================
# POST /admin/auth/change-password - Change Password Tests
# =============================================================================


def test_change_password_success(admin_auth_client):
    """Test successful password change."""
    client = admin_auth_client["client"]
    auth_service = admin_auth_client["auth_service"]

    # Add the admin with known password
    auth_service._passwords["admin@example.com"] = "currentpassword"

    resp = client.post(
        "/admin/auth/change-password",
        json={
            "current_password": "currentpassword",
            "new_password": "newpassword123",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "Password changed successfully" in body["message"]


def test_change_password_wrong_current(admin_auth_client):
    """Test change password with wrong current password returns error."""
    client = admin_auth_client["client"]

    resp = client.post(
        "/admin/auth/change-password",
        json={
            "current_password": "wrongpassword",
            "new_password": "newpassword123",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
