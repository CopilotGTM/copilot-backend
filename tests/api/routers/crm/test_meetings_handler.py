"""Tests for Meetings API handler.

Focuses on:
- Job type validation (case sensitivity, invalid values)
- Event/deal relationship validation
- Meeting status updates with valid/invalid status values
- SSE stream endpoint returns correct response type
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus

from tests.api.routers.crm.conftest import create_meeting

# =============================================================================
# GET /{deal_id}/meetings - List Meetings Tests
# =============================================================================


def test_list_meetings_empty(client: TestClient, meetings_dependency_overrides):
    """Test listing meetings for deal with no meetings."""
    deal_id = uuid4()

    resp = client.get(f"/users/crm/deals/{deal_id}/meetings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"] == []


def test_list_meetings_success(client: TestClient, meetings_dependency_overrides):
    """Test listing meetings returns items correctly."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    # Add meetings
    for i in range(3):
        meeting = create_meeting(deal_id, org_id, title=f"Meeting {i}")
        meetings_service.add_meeting(meeting)

    resp = client.get(f"/users/crm/deals/{deal_id}/meetings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 3


def test_list_meetings_multiple_deals_isolation(client: TestClient, meetings_dependency_overrides):
    """Test that meetings are isolated per deal."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]

    deal_1 = uuid4()
    deal_2 = uuid4()

    # Add meetings to deal_1
    for i in range(3):
        meeting = create_meeting(deal_1, org_id, title=f"Deal1 Meeting {i}")
        meetings_service.add_meeting(meeting)

    # Add meetings to deal_2
    meeting = create_meeting(deal_2, org_id, title="Deal2 Meeting")
    meetings_service.add_meeting(meeting)

    # List deal_1 meetings
    resp1 = client.get(f"/users/crm/deals/{deal_1}/meetings")
    assert resp1.status_code == 200
    assert len(resp1.json()["data"]) == 3

    # List deal_2 meetings
    resp2 = client.get(f"/users/crm/deals/{deal_2}/meetings")
    assert resp2.status_code == 200
    assert len(resp2.json()["data"]) == 1


# =============================================================================
# POST /{deal_id}/meetings/{event_id}/jobs/{job_type} - Initiate Job Tests
# =============================================================================


def test_initiate_briefing_job_success(client: TestClient, meetings_dependency_overrides):
    """Test initiating a briefing job successfully."""
    job_service = meetings_dependency_overrides["job_service"]
    deal_id = uuid4()
    event_id = uuid4()

    # Mark event as valid
    job_service.add_valid_event(event_id, deal_id)

    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/briefing")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "job_id" in body["data"]
    assert body["data"]["job_type"] == "meeting_briefing"


def test_initiate_summary_job_success(client: TestClient, meetings_dependency_overrides):
    """Test initiating a summary job successfully."""
    job_service = meetings_dependency_overrides["job_service"]
    deal_id = uuid4()
    event_id = uuid4()

    job_service.add_valid_event(event_id, deal_id)

    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/summary")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["data"]["job_type"] == "meeting_summary"


def test_initiate_followup_job_success(client: TestClient, meetings_dependency_overrides):
    """Test initiating a followup job successfully."""
    job_service = meetings_dependency_overrides["job_service"]
    deal_id = uuid4()
    event_id = uuid4()

    job_service.add_valid_event(event_id, deal_id)

    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/followup")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["data"]["job_type"] == "meeting_followup"


def test_initiate_job_invalid_job_type(client: TestClient, meetings_dependency_overrides):
    """Test initiating job with invalid job type returns 400."""
    deal_id = uuid4()
    event_id = uuid4()

    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/invalid")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "Invalid job type" in body["message"]


def test_initiate_job_type_case_sensitivity_uppercase(
    client: TestClient, meetings_dependency_overrides
):
    """Test job_type is case-sensitive - uppercase should fail."""
    deal_id = uuid4()
    event_id = uuid4()

    # BRIEFING (uppercase) should fail
    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/BRIEFING")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "Invalid job type" in body["message"]


def test_initiate_job_type_case_sensitivity_mixed(
    client: TestClient, meetings_dependency_overrides
):
    """Test job_type is case-sensitive - mixed case should fail."""
    deal_id = uuid4()
    event_id = uuid4()

    # Briefing (mixed case) should fail
    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/Briefing")
    assert resp.status_code == 400, resp.text


def test_initiate_job_type_with_spaces(client: TestClient, meetings_dependency_overrides):
    """Test job_type with spaces fails."""
    deal_id = uuid4()
    event_id = uuid4()

    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/%20briefing")
    assert resp.status_code == 400, resp.text


def test_initiate_job_event_not_found(client: TestClient, meetings_dependency_overrides):
    """Test initiating job for non-existent event returns 404."""
    deal_id = uuid4()
    event_id = uuid4()

    # Event is NOT added to valid events
    resp = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/briefing")
    assert resp.status_code == 404, resp.text


def test_initiate_job_event_wrong_deal(client: TestClient, meetings_dependency_overrides):
    """Test initiating job for event belonging to different deal returns 404."""
    job_service = meetings_dependency_overrides["job_service"]
    deal_1 = uuid4()
    deal_2 = uuid4()
    event_id = uuid4()

    # Event belongs to deal_1
    job_service.add_valid_event(event_id, deal_1)

    # Try to initiate job for deal_2
    resp = client.post(f"/users/crm/deals/{deal_2}/meetings/{event_id}/jobs/briefing")
    assert resp.status_code == 404, resp.text


def test_initiate_job_idempotent_returns_existing(
    client: TestClient, meetings_dependency_overrides
):
    """Test initiating same job twice returns existing job (idempotent)."""
    job_service = meetings_dependency_overrides["job_service"]
    deal_id = uuid4()
    event_id = uuid4()

    job_service.add_valid_event(event_id, deal_id)

    # First request
    resp1 = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/briefing")
    assert resp1.status_code == 202
    job_id_1 = resp1.json()["data"]["job_id"]

    # Second request - should return same job
    resp2 = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/briefing")
    assert resp2.status_code == 202
    job_id_2 = resp2.json()["data"]["job_id"]

    assert job_id_1 == job_id_2, "Idempotent request should return same job_id"


def test_initiate_job_different_types_creates_different_jobs(
    client: TestClient, meetings_dependency_overrides
):
    """Test initiating different job types creates separate jobs."""
    job_service = meetings_dependency_overrides["job_service"]
    deal_id = uuid4()
    event_id = uuid4()

    job_service.add_valid_event(event_id, deal_id)

    # Request briefing
    resp1 = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/briefing")
    assert resp1.status_code == 202
    job_id_briefing = resp1.json()["data"]["job_id"]

    # Request summary
    resp2 = client.post(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/summary")
    assert resp2.status_code == 202
    job_id_summary = resp2.json()["data"]["job_id"]

    assert job_id_briefing != job_id_summary, "Different job types should create different jobs"


# =============================================================================
# GET /{deal_id}/meetings/{event_id}/jobs/{job_id}/stream - SSE Stream Tests
# =============================================================================


def test_meeting_job_stream_returns_sse(
    client: TestClient, meetings_dependency_overrides, monkeypatch
):
    """Test SSE stream endpoint returns EventSourceResponse."""
    deal_id = uuid4()
    event_id = uuid4()
    job_id = uuid4()

    class FakeDealJobSSEService:
        def __init__(self, organisation_id, created_job_id, user_id):
            self.organisation_id = organisation_id
            self.job_id = created_job_id
            self.user_id = user_id

        async def ensure_user_access(self, deal_id=None):
            return True

        async def stream_events(self, *, resume_from_id=None):
            yield {"event": "complete", "data": "{}"}

        async def close(self):
            return None

    monkeypatch.setattr(
        "src.api.routers.crm.meetings_handler.DealJobSSEService",
        FakeDealJobSSEService,
    )

    resp = client.get(f"/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/{job_id}/stream")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")


# =============================================================================
# PATCH /{deal_id}/meetings/{event_id}/status - Update Status Tests
# =============================================================================


def test_update_meeting_status_to_removed(client: TestClient, meetings_dependency_overrides):
    """Test updating meeting status to REMOVED."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    meeting = create_meeting(deal_id, org_id)
    meetings_service.add_meeting(meeting)
    event_id = meeting.event_id

    payload = {"status": "REMOVED"}
    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "REMOVED" in body["message"]


def test_update_meeting_status_to_active(client: TestClient, meetings_dependency_overrides):
    """Test updating meeting status to ACTIVE."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    meeting = create_meeting(deal_id, org_id, status=AssociationStatus.REMOVED)
    meetings_service.add_meeting(meeting)
    event_id = meeting.event_id

    payload = {"status": "ACTIVE"}
    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json=payload)
    assert resp.status_code == 200, resp.text


def test_update_meeting_status_to_cancelled(client: TestClient, meetings_dependency_overrides):
    """Test updating meeting status to CANCELLED."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    meeting = create_meeting(deal_id, org_id)
    meetings_service.add_meeting(meeting)
    event_id = meeting.event_id

    payload = {"status": "CANCELLED"}
    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json=payload)
    assert resp.status_code == 200, resp.text


def test_update_meeting_status_invalid(client: TestClient, meetings_dependency_overrides):
    """Test updating meeting with invalid status is rejected."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    meeting = create_meeting(deal_id, org_id)
    meetings_service.add_meeting(meeting)
    event_id = meeting.event_id

    payload = {"status": "INVALID_STATUS"}
    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json=payload)
    # Should return 400 due to invalid enum value
    assert resp.status_code == 400, resp.text


def test_update_meeting_status_case_sensitivity(client: TestClient, meetings_dependency_overrides):
    """Test status is case-sensitive - lowercase should fail if enum expects uppercase."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()

    meeting = create_meeting(deal_id, org_id)
    meetings_service.add_meeting(meeting)
    event_id = meeting.event_id

    # Test lowercase (enum values are uppercase)
    payload = {"status": "removed"}
    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json=payload)
    # Should fail if strict enum validation
    assert resp.status_code == 400, f"Expected 400 for lowercase status, got {resp.status_code}"


def test_update_meeting_status_not_found(client: TestClient, meetings_dependency_overrides):
    """Test updating status for non-existent meeting returns 404."""
    deal_id = uuid4()
    event_id = uuid4()

    payload = {"status": "REMOVED"}
    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json=payload)
    assert resp.status_code == 404, resp.text


def test_update_meeting_status_wrong_deal(client: TestClient, meetings_dependency_overrides):
    """Test updating meeting status with wrong deal_id returns 404."""
    meetings_service = meetings_dependency_overrides["meetings_service"]
    org_id = meetings_dependency_overrides["user_ids"]["organisation_id"]
    deal_1 = uuid4()
    deal_2 = uuid4()

    meeting = create_meeting(deal_1, org_id)
    meetings_service.add_meeting(meeting)
    event_id = meeting.event_id

    # Try to update with wrong deal
    payload = {"status": "REMOVED"}
    resp = client.patch(f"/users/crm/deals/{deal_2}/meetings/{event_id}/status", json=payload)
    assert resp.status_code == 404, resp.text


def test_update_meeting_status_empty_body(client: TestClient, meetings_dependency_overrides):
    """Test updating meeting with empty body is rejected."""
    deal_id = uuid4()
    event_id = uuid4()

    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json={})
    assert resp.status_code == 422, resp.text


def test_update_meeting_status_null_status(client: TestClient, meetings_dependency_overrides):
    """Test updating meeting with null status is rejected."""
    deal_id = uuid4()
    event_id = uuid4()

    payload = {"status": None}
    resp = client.patch(f"/users/crm/deals/{deal_id}/meetings/{event_id}/status", json=payload)
    assert resp.status_code == 422, resp.text
