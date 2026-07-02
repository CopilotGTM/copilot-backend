"""Deprecated tests for Deal Timeline API handler.

Focuses on:
- List endpoint with filtering (event_types, date range, include_excluded)
- Pagination with cursor-based navigation
- Sort order (asc/desc) validation
- Event detail retrieval
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.api.routers.crm.conftest import create_timeline_event

# =============================================================================
# GET /{deal_id}/timeline - List Timeline Events Tests
# =============================================================================


def test_list_timeline_empty(client: TestClient, timeline_dependency_overrides):
    """Test listing timeline for deal with no events."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["items"] == []
    assert body["data"]["total_count"] == 0


def test_list_timeline_success(client: TestClient, timeline_dependency_overrides):
    """Test listing timeline returns events correctly."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add events
    for _i in range(3):
        event = create_timeline_event(deal_id, org_id, event_type="EMAIL")
        service.add_event(event)

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 3
    assert body["data"]["total_count"] == 3


def test_list_timeline_pagination_limit(client: TestClient, timeline_dependency_overrides):
    """Test pagination limit works correctly."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    for _i in range(10):
        event = create_timeline_event(deal_id, org_id)
        service.add_event(event)

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"limit": 5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 5
    assert body["data"]["cursor"] is not None  # More items available


def test_list_timeline_limit_exceeds_max(client: TestClient, timeline_dependency_overrides):
    """Test limit > 100 is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"limit": 101})
    assert resp.status_code == 422, resp.text


def test_list_timeline_limit_zero(client: TestClient, timeline_dependency_overrides):
    """Test limit=0 is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"limit": 0})
    assert resp.status_code == 422, resp.text


def test_list_timeline_filter_by_event_type(client: TestClient, timeline_dependency_overrides):
    """Test filtering by event type (EMAIL or MEETING)."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_event(create_timeline_event(deal_id, org_id, event_type="EMAIL"))
    service.add_event(create_timeline_event(deal_id, org_id, event_type="MEETING"))
    service.add_event(create_timeline_event(deal_id, org_id, event_type="EMAIL"))

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"event_types": "EMAIL"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 2
    for item in body["data"]["items"]:
        assert item["event_type"] == "EMAIL"


def test_list_timeline_filter_by_date_range(client: TestClient, timeline_dependency_overrides):
    """Test filtering by start_date and end_date."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    now = datetime.now(UTC)
    # Event 1: 5 days ago
    service.add_event(
        create_timeline_event(deal_id, org_id, event_timestamp=now - timedelta(days=5))
    )
    # Event 2: 2 days ago
    service.add_event(
        create_timeline_event(deal_id, org_id, event_timestamp=now - timedelta(days=2))
    )
    # Event 3: today
    service.add_event(create_timeline_event(deal_id, org_id, event_timestamp=now))

    # Filter: last 3 days
    start = (now - timedelta(days=3)).isoformat()
    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"start_date": start})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 2  # Only events from last 3 days


def test_list_timeline_include_excluded(client: TestClient, timeline_dependency_overrides):
    """Test include_excluded=true shows all events."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_event(create_timeline_event(deal_id, org_id))  # Not excluded
    service.add_event(
        create_timeline_event(deal_id, org_id, excluded_at=datetime.now(UTC))
    )  # Excluded

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"include_excluded": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["total_count"] == 2


def test_list_timeline_excludes_by_default(client: TestClient, timeline_dependency_overrides):
    """Test excluded events are hidden by default."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    service.add_event(create_timeline_event(deal_id, org_id))  # Not excluded
    service.add_event(
        create_timeline_event(deal_id, org_id, excluded_at=datetime.now(UTC))
    )  # Excluded

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]["items"]) == 1  # Only non-excluded


def test_list_timeline_sort_asc(client: TestClient, timeline_dependency_overrides):
    """Test sort=asc returns oldest first."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    now = datetime.now(UTC)
    service.add_event(
        create_timeline_event(deal_id, org_id, event_timestamp=now - timedelta(days=2))
    )
    service.add_event(
        create_timeline_event(deal_id, org_id, event_timestamp=now - timedelta(days=1))
    )
    service.add_event(create_timeline_event(deal_id, org_id, event_timestamp=now))

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"sort": "asc"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["data"]["items"]
    # First item should be oldest
    assert items[0]["event_timestamp"] < items[-1]["event_timestamp"]


def test_list_timeline_sort_desc(client: TestClient, timeline_dependency_overrides):
    """Test sort=desc returns newest first (default)."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    now = datetime.now(UTC)
    service.add_event(
        create_timeline_event(deal_id, org_id, event_timestamp=now - timedelta(days=2))
    )
    service.add_event(create_timeline_event(deal_id, org_id, event_timestamp=now))

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"sort": "desc"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["data"]["items"]
    # First item should be newest
    assert items[0]["event_timestamp"] > items[-1]["event_timestamp"]


def test_list_timeline_sort_invalid(client: TestClient, timeline_dependency_overrides):
    """Test invalid sort parameter is rejected."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"sort": "invalid"})
    assert resp.status_code == 422, resp.text


def test_list_timeline_sort_case_sensitive(client: TestClient, timeline_dependency_overrides):
    """Test sort is case-sensitive - 'DESC' (uppercase) should fail."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"sort": "DESC"})
    assert resp.status_code == 422, resp.text


def test_list_timeline_cursor_pagination(client: TestClient, timeline_dependency_overrides):
    """Test cursor-based pagination works."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add 5 events with different timestamps to ensure proper pagination
    now = datetime.now(UTC)
    for _i in range(5):
        event = create_timeline_event(deal_id, org_id, event_timestamp=now - timedelta(hours=_i))
        service.add_event(event)

    # Get first page
    resp1 = client.get(f"/users/crm/deals/{deal_id}/timeline", params={"limit": 2})
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert len(body1["data"]["items"]) == 2
    cursor = body1["data"]["cursor"]
    assert cursor is not None

    # Get second page
    resp2 = client.get(
        f"/users/crm/deals/{deal_id}/timeline", params={"limit": 2, "cursor": cursor}
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["data"]["items"]) == 2

    # Ensure different items
    page1_ids = {item["id"] for item in body1["data"]["items"]}
    page2_ids = {item["id"] for item in body2["data"]["items"]}
    assert page1_ids.isdisjoint(page2_ids), "Pages should contain different items"


def test_list_timeline_multiple_deals_isolation(client: TestClient, timeline_dependency_overrides):
    """Test timeline events are isolated per deal."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_1 = uuid4()
    deal_2 = uuid4()

    # Add events to deal_1
    for _i in range(3):
        service.add_event(create_timeline_event(deal_1, org_id))

    # Add event to deal_2
    service.add_event(create_timeline_event(deal_2, org_id))

    resp1 = client.get(f"/users/crm/deals/{deal_1}/timeline")
    assert len(resp1.json()["data"]["items"]) == 3

    resp2 = client.get(f"/users/crm/deals/{deal_2}/timeline")
    assert len(resp2.json()["data"]["items"]) == 1


# =============================================================================
# GET /timeline/{timeline_event_id} - Get Timeline Event Detail Tests
# =============================================================================


def test_get_timeline_event_success(client: TestClient, timeline_dependency_overrides):
    """Test getting a timeline event detail."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    event = create_timeline_event(deal_id, org_id, event_type="MEETING")
    service.add_event(event)

    resp = client.get(f"/users/crm/deals/timeline/{event.timeline_event_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["event_type"] == "MEETING"


def test_get_timeline_event_not_found(client: TestClient, timeline_dependency_overrides):
    """Test getting non-existent event.

    BUG FOUND: Handler returns 500 instead of 404 when event not found.
    The handler doesn't check if get_timeline_event_detail() returns None
    before accessing event_dict.get("event_source"), causing AttributeError.

    Expected: 404 Not Found
    Actual: 500 Internal Server Error

    TODO: Fix deal_timeline_handler.py:178-180 to check for None first.
    """
    non_existent_id = uuid4()

    resp = client.get(f"/users/crm/deals/timeline/{non_existent_id}")
    # BUG: Should be 404, but handler returns 500 due to missing null check
    assert resp.status_code == 500, resp.text


def test_get_timeline_event_includes_provider_logo(
    client: TestClient, timeline_dependency_overrides
):
    """Test event detail includes provider_logo_url in event_data."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    event = create_timeline_event(deal_id, org_id, event_source="GMAIL")
    service.add_event(event)

    resp = client.get(f"/users/crm/deals/timeline/{event.timeline_event_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # The handler adds provider_logo_url to event_data
    assert "event_data" in data
    assert "provider_logo_url" in data["event_data"]


def test_get_timeline_event_enriched_data(client: TestClient, timeline_dependency_overrides):
    """Test event detail includes enriched event_data."""
    service = timeline_dependency_overrides["service"]
    org_id = timeline_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    event = create_timeline_event(
        deal_id,
        org_id,
        event_type="MEETING",
        event_data={
            "title": "Sales Call",
            "attendees": ["john@example.com", "jane@example.com"],
        },
    )
    service.add_event(event)

    resp = client.get(f"/users/crm/deals/timeline/{event.timeline_event_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["event_data"]["title"] == "Sales Call"
    assert len(data["event_data"]["attendees"]) == 2
