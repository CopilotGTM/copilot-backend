"""Tests for Deal Notes API handler.

Focuses on:
- Input validation (empty body, max lengths, whitespace)
- Pagination edge cases
- Error handling (deal not found)
- Note source detection (user vs CRM)
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.api.schemas.crm_note_models import CreateNoteRequest, UpdateNoteRequest

from tests.api.routers.crm.conftest import create_note


def test_note_request_models_use_title_and_body_contract():
    create = CreateNoteRequest.model_validate({"body": "  Note body  ", "title": "  Title  "})
    assert create.body == "Note body"
    assert create.title == "Title"

    update = UpdateNoteRequest.model_validate({"body": "  Updated body  ", "title": "   "})
    assert update.body == "Updated body"
    assert update.title is None

    try:
        CreateNoteRequest.model_validate({"content": "Legacy body", "subject": "Legacy title"})
    except ValidationError as exc:
        errors = exc.errors()
    else:
        raise AssertionError("Legacy content/subject payload should be rejected")

    assert any(error["loc"] == ("body",) and error["type"] == "missing" for error in errors)
    assert any(
        error["loc"] == ("content",) and error["type"] == "extra_forbidden" for error in errors
    )


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


def test_list_notes_display_title_falls_back_to_body_preview(
    client: TestClient, notes_dependency_overrides
):
    service = notes_dependency_overrides["service"]
    org_id = notes_dependency_overrides["user_ids"]["organisation_id"]
    deal_id = uuid4()
    service.add_deal(deal_id)
    service.add_note(
        create_note(
            deal_id,
            org_id,
            content="This body becomes the visible note title",
            subject=None,
        )
    )

    resp = client.get(f"/users/crm/deals/{deal_id}/notes")

    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["title"] is None
    assert item["body"] == "This body becomes the visible note title"
    assert item["display_title"] == "This body becomes the visible"


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

    payload = {"body": "This is a test note."}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["body"] == "This is a test note."
    assert data["title"] is None
    assert data["display_title"] == "This is a test note."
    assert data["is_user_created"] is True
    assert data["source"] == "user"


def test_create_note_with_title(client: TestClient, notes_dependency_overrides):
    """Test creating a note with title."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"body": "Note content", "title": "Important Update"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    data = body["data"]
    assert data["title"] == "Important Update"
    assert data["display_title"] == "Important Update"


def test_create_note_duplicate_returns_existing_note(
    client: TestClient,
    notes_dependency_overrides,
):
    """Test duplicate note create returns existing note and does not create a new row."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"body": "Same content", "title": "Same title"}
    first = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert first.status_code == 201, first.text
    first_body = first.json()

    second = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert second.status_code == 200, second.text
    second_body = second.json()

    assert second_body["message"] == "Identical note already exists; duplicate was not created"
    assert second_body["data"]["note_id"] == first_body["data"]["note_id"]
    assert len(service._notes.get(deal_id, [])) == 1


def test_create_note_same_body_different_title_creates_new(
    client: TestClient,
    notes_dependency_overrides,
):
    """Test same body with different title is treated as a new note."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    first_payload = {"body": "Same content", "title": "Title A"}
    second_payload = {"body": "Same content", "title": "Title B"}

    first = client.post(f"/users/crm/deals/{deal_id}/notes", json=first_payload)
    assert first.status_code == 201, first.text

    second = client.post(f"/users/crm/deals/{deal_id}/notes", json=second_payload)
    assert second.status_code == 201, second.text

    first_body = first.json()
    second_body = second.json()
    assert first_body["data"]["note_id"] != second_body["data"]["note_id"]
    assert len(service._notes.get(deal_id, [])) == 2


def test_create_note_deal_not_found(client: TestClient, notes_dependency_overrides):
    """Test creating note for non-existent deal returns error."""
    non_existent_deal = uuid4()
    payload = {"body": "Test content"}
    resp = client.post(f"/users/crm/deals/{non_existent_deal}/notes", json=payload)
    assert resp.status_code in (404, 500), resp.text


def test_create_note_empty_body(client: TestClient, notes_dependency_overrides):
    """Test creating note with empty body is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"body": ""}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_note_whitespace_only_body(client: TestClient, notes_dependency_overrides):
    """Test creating note with whitespace-only body is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    for bad_body in ["   ", "\n\t  ", "  \n  "]:
        payload = {"body": bad_body}
        resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
        assert resp.status_code == 422, (
            f"Expected 422 for body='{bad_body!r}', got {resp.status_code}"
        )


def test_create_note_body_max_length(client: TestClient, notes_dependency_overrides):
    """Test creating note with exactly max length body succeeds."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Max length is 10000
    payload = {"body": "x" * 10000}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text


def test_create_note_body_exceeds_max(client: TestClient, notes_dependency_overrides):
    """Test creating note with body exceeding max length is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Exceeds max length of 10000
    payload = {"body": "x" * 10001}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_note_title_max_length(client: TestClient, notes_dependency_overrides):
    """Test creating note with exactly max title length succeeds."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"body": "Note content", "title": "x" * 500}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text


def test_create_note_title_exceeds_max(client: TestClient, notes_dependency_overrides):
    """Test creating note with title exceeding max length is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"body": "Note content", "title": "x" * 501}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text


def test_create_note_title_whitespace_only(client: TestClient, notes_dependency_overrides):
    """Test title with only whitespace returns the body preview fallback."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"body": "Note content", "title": "   "}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["data"]["title"] is None
    assert body["data"]["display_title"] == "Note content"


def test_create_note_special_characters(client: TestClient, notes_dependency_overrides):
    """Test creating note with special characters (XSS attempt)."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Test XSS-like body is stored safely
    payload = {"body": "<script>alert('xss')</script>"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Body should be stored as-is (escaping happens on render)
    assert "<script>" in body["data"]["body"]


def test_create_note_unicode_content(client: TestClient, notes_dependency_overrides):
    """Test creating note with unicode characters."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    # Test various unicode characters
    payload = {"body": "Hello \U0001f44b \u4e2d\u6587 \u0645\u0631\u062d\u0628\u0627"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text


def test_create_note_author_populated(client: TestClient, notes_dependency_overrides):
    """Test created note has author information from user."""
    service = notes_dependency_overrides["service"]
    user_id = notes_dependency_overrides["user_ids"]["user_id"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"body": "Test note"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    author = body["data"]["author"]
    assert author["id"] == str(user_id)
    assert author["name"] == "Test User"
    assert author["email"] == "test@example.com"


def test_note_presenter_uses_metadata_author_id_when_author_user_id_missing():
    """Test presenter keeps API author.id stable for app-created notes."""
    from src.api.presenters.crm.note_presenter import present_note_item

    user_id = uuid4()
    deal_id = uuid4()
    org_id = uuid4()
    note = create_note(
        deal_id,
        org_id,
        content="Body used for preview title",
        subject=None,
    )
    note.note_metadata = {"created_by_public_user_id": str(user_id)}

    item = present_note_item(note)

    assert item.title is None
    assert item.body == "Body used for preview title"
    assert item.display_title == "Body used for preview title"
    assert item.author.id == str(user_id)
    assert item.source == "user"


def test_create_note_missing_body_field(client: TestClient, notes_dependency_overrides):
    """Test creating note without body field is rejected."""
    service = notes_dependency_overrides["service"]
    deal_id = uuid4()
    service.add_deal(deal_id)

    payload = {"title": "Title only"}
    resp = client.post(f"/users/crm/deals/{deal_id}/notes", json=payload)
    assert resp.status_code == 422, resp.text
