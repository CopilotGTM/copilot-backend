"""Tests for Admin Organizations API handler.

Focuses on:
- List countries endpoint
- Create organisation endpoint
- List organisations endpoint
- Get single organisation endpoint
- Update organisation endpoint
- Update organisation status endpoint
- Get organisation history endpoint
"""

from uuid import uuid4

# =============================================================================
# GET /admin/organisations/countries - List Countries Tests
# =============================================================================


def test_list_countries_success(admin_org_client):
    """Test listing allowed countries returns list."""
    client = admin_org_client["client"]

    resp = client.get("/admin/organisations/countries")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "countries" in body["data"]
    assert isinstance(body["data"]["countries"], list)


# =============================================================================
# POST /admin/organisations/create_organisation - Create Organisation Tests
# =============================================================================


def test_create_organisation_accepted(admin_org_client):
    """Test creating organisation returns 202 Accepted with job_id."""
    client = admin_org_client["client"]

    resp = client.post(
        "/admin/organisations/create_organisation",
        json={
            "organisation_name": "New Test Org",
            "country_code": "US",
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "job_id" in body["data"]
    assert body["data"]["status"] == "pending"


def test_create_organisation_with_details(admin_org_client):
    """Test creating organisation with additional details."""
    client = admin_org_client["client"]

    resp = client.post(
        "/admin/organisations/create_organisation",
        json={
            "organisation_name": "Detailed Org",
            "country_code": "US",
            "industry": "Technology",
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_create_organisation_missing_name(admin_org_client):
    """Test creating organisation without name returns 422."""
    client = admin_org_client["client"]

    resp = client.post(
        "/admin/organisations/create_organisation",
        json={"country_code": "US"},
    )
    assert resp.status_code == 422, resp.text


# =============================================================================
# GET /admin/organisations/list_organisations - List Organisations Tests
# =============================================================================


def test_list_organisations_empty(admin_org_client):
    """Test listing organisations with none returns empty list."""
    client = admin_org_client["client"]

    resp = client.get("/admin/organisations/list_organisations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "organizations" in body["data"]


def test_list_organisations_success(admin_org_client):
    """Test listing organisations returns list."""
    client = admin_org_client["client"]
    service = admin_org_client["service"]

    service.add_organisation(organisation_name="Org 1")
    service.add_organisation(organisation_name="Org 2")

    resp = client.get("/admin/organisations/list_organisations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["organizations"]) == 2


def test_list_organisations_pagination(admin_org_client):
    """Test listing organisations with limit parameter."""
    client = admin_org_client["client"]
    service = admin_org_client["service"]

    for i in range(5):
        service.add_organisation(organisation_name=f"Org {i}")

    resp = client.get("/admin/organisations/list_organisations", params={"limit": 3})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert len(body["data"]["organizations"]) == 3


def test_list_organisations_limit_below_min(admin_org_client):
    """Test listing organisations with limit=0 returns 422."""
    client = admin_org_client["client"]

    resp = client.get("/admin/organisations/list_organisations", params={"limit": 0})
    assert resp.status_code == 422, resp.text


def test_list_organisations_limit_above_max(admin_org_client):
    """Test listing organisations with limit=101 returns 422."""
    client = admin_org_client["client"]

    resp = client.get("/admin/organisations/list_organisations", params={"limit": 101})
    assert resp.status_code == 422, resp.text


# =============================================================================
# GET /admin/organisations/{organisation_id} - Get Organisation Tests
# =============================================================================


def test_get_organisation_success(admin_org_client):
    """Test getting a single organisation."""
    client = admin_org_client["client"]
    service = admin_org_client["service"]

    org = service.add_organisation(organisation_name="Test Org")

    resp = client.get(f"/admin/organisations/{org.organisation_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["organisation_name"] == "Test Org"


def test_get_organisation_not_found(admin_org_client):
    """Test getting non-existent organisation returns 404."""
    client = admin_org_client["client"]
    non_existent_id = uuid4()

    resp = client.get(f"/admin/organisations/{non_existent_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_get_organisation_invalid_uuid(admin_org_client):
    """Test getting organisation with invalid UUID returns 422."""
    client = admin_org_client["client"]

    resp = client.get("/admin/organisations/not-a-uuid")
    assert resp.status_code == 422, resp.text


# =============================================================================
# PUT /admin/organisations/{organisation_id}/update - Update Organisation Tests
# =============================================================================


def test_update_organisation_success(admin_org_client):
    """Test updating organisation details."""
    client = admin_org_client["client"]
    service = admin_org_client["service"]

    org = service.add_organisation(organisation_name="Original Name")

    resp = client.put(
        f"/admin/organisations/{org.organisation_id}/update",
        json={"organisation_name": "Updated Name"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_update_organisation_not_found(admin_org_client):
    """Test updating non-existent organisation returns error."""
    client = admin_org_client["client"]
    non_existent_id = uuid4()

    resp = client.put(
        f"/admin/organisations/{non_existent_id}/update",
        json={"organisation_name": "New Name"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_update_organisation_no_data(admin_org_client):
    """Test updating organisation with empty data returns error."""
    client = admin_org_client["client"]
    service = admin_org_client["service"]

    org = service.add_organisation(organisation_name="Test Org")

    resp = client.put(
        f"/admin/organisations/{org.organisation_id}/update",
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# =============================================================================
# PUT /admin/organisations/{organisation_id}/status - Update Status Tests
# =============================================================================


def test_update_organisation_status_success(admin_org_client):
    """Test updating organisation status."""
    client = admin_org_client["client"]
    service = admin_org_client["service"]

    org = service.add_organisation(organisation_name="Test Org", status="pending")

    resp = client.put(
        f"/admin/organisations/{org.organisation_id}/status",
        json={"status": "approved"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_update_organisation_status_not_found(admin_org_client):
    """Test updating status for non-existent organisation returns error."""
    client = admin_org_client["client"]
    non_existent_id = uuid4()

    resp = client.put(
        f"/admin/organisations/{non_existent_id}/status",
        json={"status": "approved"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# =============================================================================
# GET /admin/organisations/{organisation_id}/history - Get History Tests
# =============================================================================


def test_get_organisation_history_success(admin_org_client):
    """Test getting organisation history."""
    client = admin_org_client["client"]
    service = admin_org_client["service"]

    org = service.add_organisation(organisation_name="Test Org")

    resp = client.get(f"/admin/organisations/{org.organisation_id}/history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "history" in body["data"]


def test_get_organisation_history_not_found(admin_org_client):
    """Test getting history for non-existent organisation returns error."""
    client = admin_org_client["client"]
    non_existent_id = uuid4()

    resp = client.get(f"/admin/organisations/{non_existent_id}/history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# =============================================================================
# GET /admin/organisations/all_organisations_history - All History Tests
# =============================================================================


def test_get_all_organisations_history(admin_org_client):
    """Test getting history for all organisations."""
    client = admin_org_client["client"]

    resp = client.get("/admin/organisations/all_organisations_history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "history" in body["data"]
