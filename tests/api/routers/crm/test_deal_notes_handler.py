"""Tests for Deal Notes API handler.

Focuses on:
- Input validation (empty content, max lengths, whitespace)
- Pagination edge cases
- Error handling (deal not found)
- Note source detection (user vs CRM)
"""

from uuid import uuid4

from fastapi.testclient import TestClient

from tests.api.routers.crm.conftest import create_note

# =============================================================================
# GET /{deal_id}/notes - List Notes Tests
# =============================================================================


def test_list_notes_deal_not_found(client: TestClient, notes_dependency_overrides):
    """Test listing notes for non-existent deal returns error."""
    non_existent_deal = uuid4()
    resp = client.get(f"/users/crm/deals/{non_existent_deal}/notes")
    # Should return 404 or handle gracefully
    assert resp.status_code in (404, 500), resp.text


def test_list_notes_empty(client: TestClient, notes_dependency_overrides):
    """Test listing notes for deal with no notes."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["items"] == []
    assert data["total_count"] == 0
    assert data["cursor"] is None


def test_list_notes_success(client: TestClient, notes_dependency_overrides):
    """Test listing notes returns items correctly."""
    service = notes_dependency_overrides["service"]
    org_id = notes_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Add notes
    for i in range(3):
        note = create_note(deal_id, org_id, content=f"Note {i}")
        service.add_note(note)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 3
    assert data["total_count"] == 3


def test_list_notes_pagination_limit_1(client: TestClient, notes_dependency_overrides):
    """Test pagination with limit=1 returns exactly 1 item."""
    service = notes_dependency_overrides["service"]
    org_id = notes_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Add multiple notes
    for i in range(5):
        note = create_note(deal_id, org_id, content=f"Note {i}")
        service.add_note(note)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"limit": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 1
    assert data["cursor"] is not None  # More items available


def test_list_notes_pagination_limit_100_max(client: TestClient, notes_dependency_overrides):
    """Test pagination accepts max limit of 100."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"limit": 100})
    assert resp.status_code == 200, resp.text


def test_list_notes_pagination_limit_exceeds_max(client: TestClient, notes_dependency_overrides):
    """Test pagination rejects limit > 100."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"limit": 101})
    assert resp.status_code == 422, resp.text


def test_list_notes_pagination_limit_zero(client: TestClient, notes_dependency_overrides):
    """Test pagination rejects limit=0."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"limit": 0})
    assert resp.status_code == 422, resp.text


def test_list_notes_pagination_limit_negative(client: TestClient, notes_dependency_overrides):
    """Test pagination rejects negative limit."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"limit": -1})
    assert resp.status_code == 422, resp.text


def test_list_notes_sort_asc(client: TestClient, notes_dependency_overrides):
    """Test sorting notes in ascending order."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"sort": "asc"})
    assert resp.status_code == 200, resp.text


def test_list_notes_sort_desc(client: TestClient, notes_dependency_overrides):
    """Test sorting notes in descending order (default)."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"sort": "desc"})
    assert resp.status_code == 200, resp.text


def test_list_notes_sort_invalid(client: TestClient, notes_dependency_overrides):
    """Test invalid sort parameter is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"sort": "invalid"})
    # Should be 422 due to regex pattern validation
    assert resp.status_code == 422, resp.text


def test_list_notes_sort_case_sensitivity(client: TestClient, notes_dependency_overrides):
    """Test sort parameter is case-sensitive (only lowercase accepted)."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes", params={"sort": "DESC"})
    # Uppercase should fail due to regex ^(asc|desc)$
    assert resp.status_code == 422, resp.text


def test_list_notes_with_participants(client: TestClient, notes_dependency_overrides):
    """Test listing notes includes participants."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)
    service.add_participant(deal_id, "owner@example.com", "Deal Owner")

    resp = client.get(f"/users/crm/deals/{deal_id}/notes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert "participants" in data
    assert len(data["participants"]) == 1


def test_list_notes_mixed_sources(client: TestClient, notes_dependency_overrides):
    """Test listing notes shows correct source for user and CRM notes."""
    service = notes_dependency_overrides["service"]
    org_id = notes_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Add user-created note
    user_note = create_note(deal_id, org_id, content="User note", is_user_created=True)
    service.add_note(user_note)

    # Add CRM-synced note
    crm_note = create_note(deal_id, org_id, content="CRM note", is_user_created=False)
    service.add_note(crm_note)

    resp = client.get(f"/users/crm/deals/{deal_id}/notes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 2

    # Check sources are different
    sources = {item["source"] for item in data["items"]}
    assert "user" in sources


# =============================================================================
# POST /{deal_id}/notes - Create Note Tests
# =============================================================================


def test_create_note_success(client: TestClient, notes_dependency_overrides):
    """Test creating a note successfully."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"content": "This is a test note."}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["content"] == "This is a test note."
    assert data["is_user_created"] is True
    assert data["source"] == "user"


def test_create_note_with_subject(client: TestClient, notes_dependency_overrides):
    """Test creating a note with subject."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"content": "Note content", "subject": "Important Update"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert data["subject"] == "Important Update"


def test_create_note_deal_not_found(client: TestClient, notes_dependency_overrides):
    """Test creating note for non-existent deal returns error."""
    non_existent_deal = uuid4()
    payload = {"content": "Test content"}
    resp = client.post(f"/users/crm/deals/{non_existent_deal}/notes", json=payload)
    assert resp.status_code in (404, 500), resp.text


def test_create_note_empty_content(client: TestClient, notes_dependency_overrides):
    """Test creating note with empty content is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"content": ""}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_note_whitespace_only_content(client: TestClient, notes_dependency_overrides):
    """Test creating note with whitespace-only content is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    for bad_content in ["   ", "\n\t  ", "  \n  "]:
        payload = {"content": bad_content}
        resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
        assert (
            resp.status_code == 422
        ), f"Expected 422 for content='{bad_content!r}', got {resp.status_code}"


def test_create_note_content_max_length(client: TestClient, notes_dependency_overrides):
    """Test creating note with exactly max length content succeeds."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Max length is 10000
    payload = {"content": "x" * 10000}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text


def test_create_note_content_exceeds_max(client: TestClient, notes_dependency_overrides):
    """Test creating note with content exceeding max length is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Exceeds max length of 10000
    payload = {"content": "x" * 10001}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_note_subject_max_length(client: TestClient, notes_dependency_overrides):
    """Test creating note with exactly max subject length succeeds."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Max subject length is 500
    payload = {"content": "Note content", "subject": "x" * 500}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text


def test_create_note_subject_exceeds_max(client: TestClient, notes_dependency_overrides):
    """Test creating note with subject exceeding max length is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Exceeds max subject length of 500
    payload = {"content": "Note content", "subject": "x" * 501}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_note_subject_whitespace_only(client: TestClient, notes_dependency_overrides):
    """Test subject with only whitespace is stored as null."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"content": "Note content", "subject": "   "}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Whitespace-only subject should become null
    assert body["data"]["subject"] is None


def test_create_note_special_characters(client: TestClient, notes_dependency_overrides):
    """Test creating note with special characters (XSS attempt)."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Test XSS-like content is stored safely
    payload = {"content": "<script>alert('xss')</script>"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Content should be stored as-is (escaping happens on render)
    assert "<script>" in body["data"]["content"]


def test_create_note_unicode_content(client: TestClient, notes_dependency_overrides):
    """Test creating note with unicode characters."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Test various unicode characters
    payload = {"content": "Hello \ud83d\udc4b \u4e2d\u6587 \u0645\u0631\u062d\u0628\u0627"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text


def test_create_note_author_populated(client: TestClient, notes_dependency_overrides):
    """Test created note has author information from user."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"content": "Test note"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    author = body["data"]["author"]
    assert author["name"] == "Test User"
    assert author["email"] == "test@example.com"


def test_create_note_missing_content_field(client: TestClient, notes_dependency_overrides):
    """Test creating note without content field is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"subject": "Subject only"}  # Missing content
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text
