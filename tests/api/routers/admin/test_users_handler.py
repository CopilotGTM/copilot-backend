"""Tests for Admin Users API handler.

Focuses on:
- Create user endpoint
- Get user profile endpoint
- List users endpoint
- Update user admin status endpoint
- Get user action history endpoint
- Update user endpoint
"""

from uuid import uuid4

# =============================================================================
# POST /admin/users/create_user - Create User Tests
# =============================================================================


def test_create_user_success(admin_user_client):
    """Test creating a new user."""
    client = admin_user_client["client"]
    org_id = uuid4()

    resp = client.post(
        "/admin/users/create_user",
        json={
            "email": "newuser@example.com",
            "first_name": "New",
            "last_name": "User",
            "organisation_id": str(org_id),
            "user_role": "member",
            "organisation_role": "member",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["email"] == "newuser@example.com"


def test_create_user_allows_public_email_provider(admin_user_client):
    """Temporary signup policy allows Gmail and other public email providers."""
    client = admin_user_client["client"]
    org_id = uuid4()

    resp = client.post(
        "/admin/users/create_user",
        json={
            "email": "newuser@gmail.com",
            "first_name": "New",
            "last_name": "User",
            "organisation_id": str(org_id),
            "user_role": "member",
            "organisation_role": "member",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["email"] == "newuser@gmail.com"


def test_create_user_duplicate_email(admin_user_client):
    """Test creating user with duplicate email returns error."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]
    org_id = uuid4()

    # Add existing user
    service.add_user(email="existing@example.com", organisation_id=org_id)

    resp = client.post(
        "/admin/users/create_user",
        json={
            "email": "existing@example.com",
            "first_name": "Another",
            "last_name": "User",
            "organisation_id": str(org_id),
            "user_role": "member",
            "organisation_role": "member",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_create_user_missing_required_fields(admin_user_client):
    """Test creating user without required fields returns error."""
    client = admin_user_client["client"]
    org_id = uuid4()

    resp = client.post(
        "/admin/users/create_user",
        json={
            "email": "incomplete@example.com",
            "organisation_id": str(org_id),
            "user_role": "member",
        },
    )
    # Missing first_name, last_name, and organisation_role
    assert resp.status_code == 422, resp.text


def test_create_user_invalid_email(admin_user_client):
    """Test creating user with invalid email format returns 422."""
    client = admin_user_client["client"]
    org_id = uuid4()

    resp = client.post(
        "/admin/users/create_user",
        json={
            "email": "not-an-email",
            "first_name": "Test",
            "last_name": "User",
            "organisation_id": str(org_id),
            "user_role": "member",
            "organisation_role": "member",
        },
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# GET /admin/users/{user_id}/profile - Get User Profile Tests
# =============================================================================


def test_get_user_profile_success(admin_user_client):
    """Test getting user profile."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]

    user = service.add_user(email="profile@example.com")

    resp = client.get(f"/admin/users/{user.user_id}/profile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["email"] == "profile@example.com"


def test_get_user_profile_not_found(admin_user_client):
    """Test getting non-existent user profile returns error."""
    client = admin_user_client["client"]
    non_existent_id = uuid4()

    resp = client.get(f"/admin/users/{non_existent_id}/profile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_get_user_profile_invalid_uuid(admin_user_client):
    """Test getting user profile with invalid UUID returns 422."""
    client = admin_user_client["client"]

    resp = client.get("/admin/users/not-a-uuid/profile")
    assert resp.status_code == 422, resp.text


# =============================================================================
# GET /admin/users/list_users - List Users Tests
# =============================================================================


def test_list_users_empty(admin_user_client):
    """Test listing users with none returns empty list."""
    client = admin_user_client["client"]

    resp = client.get("/admin/users/list_users")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "users" in body["data"]


def test_list_users_success(admin_user_client):
    """Test listing users returns list."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]

    service.add_user(email="user1@example.com")
    service.add_user(email="user2@example.com")

    resp = client.get("/admin/users/list_users")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["users"]) == 2


def test_list_users_filter_by_organisation(admin_user_client):
    """Test listing users filtered by organisation."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]

    org1_id = uuid4()
    org2_id = uuid4()

    service.add_user(email="org1user@example.com", organisation_id=org1_id)
    service.add_user(email="org2user@example.com", organisation_id=org2_id)

    resp = client.get("/admin/users/list_users", params={"organisation_id": str(org1_id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["users"]) == 1
    assert body["data"]["users"][0]["email"] == "org1user@example.com"


def test_list_users_pagination(admin_user_client):
    """Test listing users with limit parameter."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]

    for i in range(5):
        service.add_user(email=f"user{i}@example.com")

    resp = client.get("/admin/users/list_users", params={"limit": 3})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["users"]) == 3


def test_list_users_limit_below_min(admin_user_client):
    """Test listing users with limit=0 returns 422."""
    client = admin_user_client["client"]

    resp = client.get("/admin/users/list_users", params={"limit": 0})
    assert resp.status_code == 422, resp.text


# =============================================================================
# PUT /admin/users/{user_id}/admin_status - Update User Status Tests
# =============================================================================


def test_update_user_status_success(admin_user_client):
    """Test updating user admin status."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]

    user = service.add_user(email="status@example.com", admin_status="pending")

    resp = client.put(
        f"/admin/users/{user.user_id}/admin_status",
        json={"admin_status": "approved"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_update_user_status_not_found(admin_user_client):
    """Test updating status for non-existent user returns error."""
    client = admin_user_client["client"]
    non_existent_id = uuid4()

    resp = client.put(
        f"/admin/users/{non_existent_id}/admin_status",
        json={"admin_status": "approved"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_update_user_status_with_note(admin_user_client):
    """Test updating user status with note."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]

    user = service.add_user(email="noted@example.com")

    resp = client.put(
        f"/admin/users/{user.user_id}/admin_status",
        json={"admin_status": "rejected", "note": "Did not meet requirements"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


# =============================================================================
# GET /admin/users/history/organisation/{organisation_id} - User History Tests
# =============================================================================


def test_list_user_action_history(admin_user_client):
    """Test listing user action history for organisation."""
    client = admin_user_client["client"]
    org_id = uuid4()

    resp = client.get(f"/admin/users/history/organisation/{org_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "history" in body["data"]


def test_list_user_action_history_invalid_uuid(admin_user_client):
    """Test listing history with invalid UUID returns 422."""
    client = admin_user_client["client"]

    resp = client.get("/admin/users/history/organisation/not-a-uuid")
    assert resp.status_code == 422, resp.text


# =============================================================================
# PUT /admin/users/{user_id}/update - Update User Tests
# =============================================================================


def test_update_user_success(admin_user_client):
    """Test updating user details."""
    client = admin_user_client["client"]
    service = admin_user_client["service"]

    user = service.add_user(email="update@example.com")

    resp = client.put(
        f"/admin/users/{user.user_id}/update",
        json={"first_name": "Updated", "last_name": "Name"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_update_user_not_found(admin_user_client):
    """Test updating non-existent user returns error."""
    client = admin_user_client["client"]
    non_existent_id = uuid4()

    resp = client.put(
        f"/admin/users/{non_existent_id}/update",
        json={"first_name": "New"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_update_user_invalid_uuid(admin_user_client):
    """Test updating user with invalid UUID returns 422."""
    client = admin_user_client["client"]

    resp = client.put(
        "/admin/users/not-a-uuid/update",
        json={"first_name": "Test"},
    )
    assert resp.status_code == 422, resp.text
