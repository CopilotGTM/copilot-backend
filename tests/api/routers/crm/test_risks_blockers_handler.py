"""Tests for Risks & Blockers API handler.

Focuses on:
- State machine transitions (valid and invalid)
- Input validation (max lengths, required fields)
- Filter and pagination edge cases
- CRUD operations with proper error handling
- Cross-entity isolation
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerSeverity,
    RiskBlockerStatus,
    RiskBlockerType,
)

from tests.api.routers.crm.conftest import create_risk_blocker

# =============================================================================
# GET /{deal_id}/risks-blockers - List Tests
# =============================================================================


def test_list_risks_blockers_empty(client: TestClient, risks_dependency_overrides):
    """Test listing risks/blockers for deal with none."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["items"] == []
    assert data["total_count"] == 0


def test_list_risks_blockers_success(client: TestClient, risks_dependency_overrides):
    """Test listing risks/blockers returns items correctly."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add items
    for i in range(3):
        item = create_risk_blocker(deal_id, org_id, title=f"Risk {i}")
        service.add_item(item)

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 3
    assert data["total_count"] == 3


def test_list_risks_blockers_filter_by_type_risk(client: TestClient, risks_dependency_overrides):
    """Test filtering by RISK type."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add mixed types
    service.add_item(create_risk_blocker(deal_id, org_id, item_type=RiskBlockerType.RISK))
    service.add_item(create_risk_blocker(deal_id, org_id, item_type=RiskBlockerType.BLOCKER))
    service.add_item(create_risk_blocker(deal_id, org_id, item_type=RiskBlockerType.RISK))

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"item_type": "RISK"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 2
    for item in data["items"]:
        assert item["item_type"] == "RISK"


def test_list_risks_blockers_filter_by_type_blocker(client: TestClient, risks_dependency_overrides):
    """Test filtering by BLOCKER type."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_item(create_risk_blocker(deal_id, org_id, item_type=RiskBlockerType.RISK))
    service.add_item(create_risk_blocker(deal_id, org_id, item_type=RiskBlockerType.BLOCKER))

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"item_type": "BLOCKER"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 1


def test_list_risks_blockers_filter_by_severity(client: TestClient, risks_dependency_overrides):
    """Test filtering by severity."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_item(create_risk_blocker(deal_id, org_id, severity=RiskBlockerSeverity.CRITICAL))
    service.add_item(create_risk_blocker(deal_id, org_id, severity=RiskBlockerSeverity.LOW))

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"severity": "CRITICAL"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 1


def test_list_risks_blockers_filter_by_status_comma_separated(
    client: TestClient, risks_dependency_overrides
):
    """Test filtering by comma-separated statuses."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN))
    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.IN_PROGRESS))
    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.RESOLVED))

    resp = client.get(
        f"/users/crm/deals/{deal_id}/risks-blockers",
        params={"status": "OPEN,IN_PROGRESS", "include_resolved": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 2


def test_list_risks_blockers_excludes_resolved_by_default(
    client: TestClient, risks_dependency_overrides
):
    """Test that resolved items are excluded by default."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN))
    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.RESOLVED))
    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.DISMISSED))

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 1  # Only OPEN


def test_list_risks_blockers_include_resolved(client: TestClient, risks_dependency_overrides):
    """Test include_resolved shows all items."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN))
    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.RESOLVED))

    resp = client.get(
        f"/users/crm/deals/{deal_id}/risks-blockers", params={"include_resolved": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    # Summary should still reflect all items
    assert data["summary"]["total_count"] == 2


def test_list_risks_blockers_pagination_limit(client: TestClient, risks_dependency_overrides):
    """Test pagination limit works correctly."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    for i in range(10):
        service.add_item(create_risk_blocker(deal_id, org_id, title=f"Risk {i}"))

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"limit": 5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 5


def test_list_risks_blockers_limit_exceeds_max(client: TestClient, risks_dependency_overrides):
    """Test limit > 100 is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"limit": 101})
    assert resp.status_code == 422, resp.text


def test_list_risks_blockers_limit_zero(client: TestClient, risks_dependency_overrides):
    """Test limit=0 is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"limit": 0})
    assert resp.status_code == 422, resp.text


def test_list_risks_blockers_sort_invalid(client: TestClient, risks_dependency_overrides):
    """Test invalid sort parameter is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"sort": "invalid"})
    assert resp.status_code == 422, resp.text


def test_list_risks_blockers_sort_case_sensitive(client: TestClient, risks_dependency_overrides):
    """Test sort is case-sensitive (uppercase should fail)."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/risks-blockers", params={"sort": "DESC"})
    assert resp.status_code == 422, resp.text


def test_list_risks_blockers_summary_counts(client: TestClient, risks_dependency_overrides):
    """Test summary counts are correct."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add diverse items
    service.add_item(
        create_risk_blocker(
            deal_id, org_id, item_type=RiskBlockerType.RISK, severity=RiskBlockerSeverity.CRITICAL
        )
    )
    service.add_item(
        create_risk_blocker(
            deal_id, org_id, item_type=RiskBlockerType.BLOCKER, severity=RiskBlockerSeverity.HIGH
        )
    )
    service.add_item(create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN))

    resp = client.get(
        f"/users/crm/deals/{deal_id}/risks-blockers", params={"include_resolved": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    summary = body["data"]["summary"]
    assert summary["risks_count"] == 2  # First and third (default type is RISK)
    assert summary["blockers_count"] == 1
    assert summary["critical_count"] == 1
    assert summary["high_count"] == 1
    assert summary["open_count"] == 3
    assert "ai_suggested_count" not in summary


# =============================================================================
# GET /risks-blockers/{risk_blocker_id} - Get Detail Tests
# =============================================================================


def test_get_risk_blocker_success(client: TestClient, risks_dependency_overrides):
    """Test getting a single risk/blocker."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, title="Test Risk")
    service.add_item(item)

    resp = client.get(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["title"] == "Test Risk"
    assert body["data"]["deal_id"] == str(deal_id)


def test_get_risk_blocker_not_found(client: TestClient, risks_dependency_overrides):
    """Test getting non-existent risk/blocker returns 404."""
    non_existent_id = uuid4()

    resp = client.get(f"/users/crm/deals/risks-blockers/{non_existent_id}")
    assert resp.status_code == 404, resp.text


# =============================================================================
# POST /{deal_id}/risks-blockers - Create Tests
# =============================================================================


def test_create_risk_success(client: TestClient, risks_dependency_overrides):
    """Test creating a risk successfully."""
    deal_id = uuid4()

    payload = {
        "item_type": "RISK",
        "title": "New Risk",
        "description": "Risk description",
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["title"] == "New Risk"
    assert data["item_type"] == "RISK"
    assert data["status"] == "OPEN"  # Manual items start as OPEN


def test_create_blocker_success(client: TestClient, risks_dependency_overrides):
    """Test creating a blocker successfully."""
    deal_id = uuid4()

    payload = {
        "item_type": "BLOCKER",
        "title": "New Blocker",
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert data["item_type"] == "BLOCKER"


def test_create_risk_with_all_fields(client: TestClient, risks_dependency_overrides):
    """Test creating a risk with all optional fields."""
    deal_id = uuid4()

    payload = {
        "item_type": "RISK",
        "title": "Complete Risk",
        "description": "Full description",
        "category": "TECHNICAL",
        "severity": "HIGH",
        "owner_name": "John Doe",
        "owner_email": "john@example.com",
        "assigned_team": "Engineering",
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert data["category"] == "TECHNICAL"
    assert data["severity"] == "HIGH"
    assert data["owner_name"] == "John Doe"


def test_create_risk_title_exceeds_max(client: TestClient, risks_dependency_overrides):
    """Test creating risk with title > 500 chars is rejected."""
    deal_id = uuid4()

    payload = {
        "item_type": "RISK",
        "title": "x" * 501,  # Exceeds 500 max
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_risk_title_exactly_max(client: TestClient, risks_dependency_overrides):
    """Test creating risk with exactly 500 char title succeeds."""
    deal_id = uuid4()

    payload = {
        "item_type": "RISK",
        "title": "x" * 500,
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 201, resp.text


def test_create_risk_missing_title(client: TestClient, risks_dependency_overrides):
    """Test creating risk without title is rejected."""
    deal_id = uuid4()

    payload = {
        "item_type": "RISK",
        # Missing title
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_risk_missing_item_type(client: TestClient, risks_dependency_overrides):
    """Test creating risk without item_type is rejected."""
    deal_id = uuid4()

    payload = {
        "title": "Some Risk",
        # Missing item_type
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_risk_invalid_item_type(client: TestClient, risks_dependency_overrides):
    """Test creating risk with invalid item_type is rejected."""
    deal_id = uuid4()

    payload = {
        "item_type": "INVALID",
        "title": "Some Risk",
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_risk_invalid_severity(client: TestClient, risks_dependency_overrides):
    """Test creating risk with invalid severity is rejected."""
    deal_id = uuid4()

    payload = {
        "item_type": "RISK",
        "title": "Risk",
        "severity": "ULTRA_HIGH",  # Invalid
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_risk_invalid_category(client: TestClient, risks_dependency_overrides):
    """Test creating risk with invalid category is rejected."""
    deal_id = uuid4()

    payload = {
        "item_type": "RISK",
        "title": "Risk",
        "category": "UNKNOWN_CATEGORY",  # Invalid
    }
    resp = client.post(f"/users/crm/deals/{deal_id}/risks-blockers", json=payload)
    assert resp.status_code == 422, resp.text


# =============================================================================
# PATCH /risks-blockers/{risk_blocker_id} - Update Tests
# =============================================================================


def test_update_risk_title(client: TestClient, risks_dependency_overrides):
    """Test updating risk title."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, title="Original Title")
    service.add_item(item)

    payload = {"title": "Updated Title"}
    resp = client.patch(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["title"] == "Updated Title"


def test_update_risk_severity(client: TestClient, risks_dependency_overrides):
    """Test updating risk severity."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, severity=RiskBlockerSeverity.LOW)
    service.add_item(item)

    payload = {"severity": "CRITICAL"}
    resp = client.patch(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["severity"] == "CRITICAL"


def test_update_risk_not_found(client: TestClient, risks_dependency_overrides):
    """Test updating non-existent risk returns 404."""
    non_existent_id = uuid4()

    payload = {"title": "Updated Title"}
    resp = client.patch(f"/users/crm/deals/risks-blockers/{non_existent_id}", json=payload)
    assert resp.status_code == 404, resp.text


def test_update_risk_empty_body(client: TestClient, risks_dependency_overrides):
    """Test updating with empty body succeeds (no changes)."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, title="Original")
    service.add_item(item)

    resp = client.patch(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}", json={})
    assert resp.status_code == 200, resp.text


# =============================================================================
# POST /risks-blockers/{id}/dismiss - Dismiss Tests
# =============================================================================


def test_dismiss_success(client: TestClient, risks_dependency_overrides):
    """Test dismissing an item."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)

    resp = client.post(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/dismiss")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["status"] == "DISMISSED"


def test_dismiss_records_closure_fields(client: TestClient, risks_dependency_overrides):
    """Test dismissing an item records closed_at and closed_by_*."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    user_ids = risks_dependency_overrides["user_ids"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)

    resp = client.post(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/dismiss")
    assert resp.status_code == 200, resp.text
    detail_resp = client.get(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}")
    detail = detail_resp.json()["data"]
    assert detail["status"] == "DISMISSED"
    assert detail["closed_at"] is not None
    assert detail["closed_by_user_id"] == str(user_ids["user_id"])


def test_dismiss_not_found(client: TestClient, risks_dependency_overrides):
    """Test dismissing non-existent item returns 404."""
    non_existent_id = uuid4()

    resp = client.post(f"/users/crm/deals/risks-blockers/{non_existent_id}/dismiss")
    assert resp.status_code == 404, resp.text


# =============================================================================
# POST /risks-blockers/{id}/resolve - Resolve Tests
# =============================================================================


def test_resolve_success(client: TestClient, risks_dependency_overrides):
    """Test resolving an item with notes records closure fields."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    user_ids = risks_dependency_overrides["user_ids"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)

    payload = {"resolution_notes": "Resolved by implementing mitigation strategy."}
    resp = client.post(
        f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/resolve", json=payload
    )
    assert resp.status_code == 200, resp.text

    detail = client.get(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}").json()["data"]
    assert detail["status"] == "RESOLVED"
    assert "mitigation" in detail["resolution_notes"]
    assert detail["closed_at"] is not None
    assert detail["closed_by_user_id"] == str(user_ids["user_id"])
    assert "resolved_by" not in detail
    assert "resolved_at" not in detail


def test_resolve_from_in_progress(client: TestClient, risks_dependency_overrides):
    """Test resolving from IN_PROGRESS status."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.IN_PROGRESS)
    service.add_item(item)

    payload = {"resolution_notes": "Completed the work to address this."}
    resp = client.post(
        f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/resolve", json=payload
    )
    assert resp.status_code == 200, resp.text


def test_resolve_notes_too_short(client: TestClient, risks_dependency_overrides):
    """Test resolving with notes < 10 chars is rejected."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)

    payload = {"resolution_notes": "Too short"}  # 9 chars
    resp = client.post(
        f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/resolve", json=payload
    )
    assert resp.status_code == 422, resp.text


def test_resolve_notes_exactly_min(client: TestClient, risks_dependency_overrides):
    """Test resolving with exactly 10 char notes succeeds."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)

    payload = {"resolution_notes": "Exactly 10"}  # 10 chars
    resp = client.post(
        f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/resolve", json=payload
    )
    assert resp.status_code == 200, resp.text


def test_resolve_missing_notes(client: TestClient, risks_dependency_overrides):
    """Test resolving without notes is rejected."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)

    resp = client.post(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/resolve", json={})
    assert resp.status_code == 422, resp.text


def test_resolve_not_found(client: TestClient, risks_dependency_overrides):
    """Test resolving non-existent item returns 404."""
    non_existent_id = uuid4()

    payload = {"resolution_notes": "This will fail."}
    resp = client.post(f"/users/crm/deals/risks-blockers/{non_existent_id}/resolve", json=payload)
    assert resp.status_code == 404, resp.text


# =============================================================================
# POST /risks-blockers/{id}/reopen - Reopen Tests
# =============================================================================


def test_reopen_resolved_success(client: TestClient, risks_dependency_overrides):
    """Test reopening a resolved item clears closure fields."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.RESOLVED)
    service.add_item(item)

    resp = client.post(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/reopen")
    assert resp.status_code == 200, resp.text

    detail = client.get(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}").json()["data"]
    assert detail["status"] == "OPEN"
    assert detail["closed_at"] is None
    assert detail["closed_by_user_id"] is None
    assert detail["closed_by_user_name"] is None
    assert "resolved_at" not in detail


def test_reopen_dismissed_success(client: TestClient, risks_dependency_overrides):
    """Test reopening a dismissed item."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.DISMISSED)
    service.add_item(item)

    resp = client.post(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/reopen")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["status"] == "OPEN"


def test_reopen_from_open_fails(client: TestClient, risks_dependency_overrides):
    """Test reopening OPEN item fails (invalid transition)."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)

    resp = client.post(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/reopen")
    assert resp.status_code == 400, resp.text


def test_reopen_from_in_progress_fails(client: TestClient, risks_dependency_overrides):
    """Test reopening IN_PROGRESS item fails (invalid transition)."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.IN_PROGRESS)
    service.add_item(item)

    resp = client.post(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}/reopen")
    assert resp.status_code == 400, resp.text


def test_reopen_not_found(client: TestClient, risks_dependency_overrides):
    """Test reopening non-existent item returns 404."""
    non_existent_id = uuid4()

    resp = client.post(f"/users/crm/deals/risks-blockers/{non_existent_id}/reopen")
    assert resp.status_code == 404, resp.text


# =============================================================================
# DELETE /risks-blockers/{risk_blocker_id} - Delete Tests
# =============================================================================


def test_delete_success(client: TestClient, risks_dependency_overrides):
    """Test deleting a risk/blocker."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id)
    service.add_item(item)

    resp = client.delete(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"] == "Risk/blocker deleted successfully"

    # Verify it's gone
    resp2 = client.get(f"/users/crm/deals/risks-blockers/{item.risk_blocker_id}")
    assert resp2.status_code == 404


def test_delete_not_found(client: TestClient, risks_dependency_overrides):
    """Test deleting non-existent item returns 404."""
    non_existent_id = uuid4()

    resp = client.delete(f"/users/crm/deals/risks-blockers/{non_existent_id}")
    assert resp.status_code == 404, resp.text


# =============================================================================
# State Machine Integration Tests
# =============================================================================


def test_full_lifecycle_ai_extracted_to_resolved(client: TestClient, risks_dependency_overrides):
    """AI-extracted items land directly as OPEN and resolve in one step."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # AI-extracted items now start as OPEN (no confirm step)
    item = create_risk_blocker(
        deal_id, org_id, status=RiskBlockerStatus.OPEN, is_ai_generated=True, title="AI Risk"
    )
    service.add_item(item)
    item_id = item.risk_blocker_id

    # Resolve directly
    payload = {"resolution_notes": "Addressed and mitigated."}
    resp = client.post(f"/users/crm/deals/risks-blockers/{item_id}/resolve", json=payload)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "RESOLVED"


def test_audit_trail_persists_across_transitions(client: TestClient, risks_dependency_overrides):
    """Audit trail must record every transition.

    This is the regression check for the JSONB mutability fix on
    DealRiskBlocker.risk_blocker_data. The model wraps the column with
    MutableDict.as_mutable(JSONB) and the service calls flag_modified, so each
    transition's audit entry persists on commit.

    Note: this test runs against the in-memory FakeRiskBlockerService, which
    mirrors the real service's audit-trail contract. End-to-end persistence
    against Postgres is verified via the smoke check in the migration runbook.
    """
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)
    item_id = item.risk_blocker_id

    client.post(
        f"/users/crm/deals/risks-blockers/{item_id}/resolve",
        json={"resolution_notes": "Mitigated for now."},
    )
    client.post(f"/users/crm/deals/risks-blockers/{item_id}/reopen")
    client.post(f"/users/crm/deals/risks-blockers/{item_id}/dismiss")

    detail = client.get(f"/users/crm/deals/risks-blockers/{item_id}").json()["data"]
    actions = [entry["action"] for entry in detail["audit_trail"]]
    assert actions == ["opened", "resolved", "reopened", "dismissed"]
    assert detail["modification_count"] == 3


def test_full_lifecycle_reopen_and_resolve_again(client: TestClient, risks_dependency_overrides):
    """Test lifecycle: OPEN -> resolve -> reopen -> resolve again."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)
    item_id = item.risk_blocker_id

    # Resolve
    resp1 = client.post(
        f"/users/crm/deals/risks-blockers/{item_id}/resolve",
        json={"resolution_notes": "First resolution."},
    )
    assert resp1.status_code == 200

    # Reopen
    resp2 = client.post(f"/users/crm/deals/risks-blockers/{item_id}/reopen")
    assert resp2.status_code == 200
    assert resp2.json()["data"]["status"] == "OPEN"

    # Resolve again
    resp3 = client.post(
        f"/users/crm/deals/risks-blockers/{item_id}/resolve",
        json={"resolution_notes": "Second resolution."},
    )
    assert resp3.status_code == 200
    assert resp3.json()["data"]["status"] == "RESOLVED"


def test_dismiss_then_reopen(client: TestClient, risks_dependency_overrides):
    """OPEN -> dismiss -> reopen workflow."""
    service = risks_dependency_overrides["service"]
    org_id = risks_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    item = create_risk_blocker(deal_id, org_id, status=RiskBlockerStatus.OPEN)
    service.add_item(item)
    item_id = item.risk_blocker_id

    # Dismiss
    resp1 = client.post(f"/users/crm/deals/risks-blockers/{item_id}/dismiss")
    assert resp1.status_code == 200
    assert resp1.json()["data"]["status"] == "DISMISSED"

    # Reopen (changed mind, want to track it)
    resp2 = client.post(f"/users/crm/deals/risks-blockers/{item_id}/reopen")
    assert resp2.status_code == 200
    assert resp2.json()["data"]["status"] == "OPEN"
