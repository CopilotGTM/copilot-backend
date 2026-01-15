"""Tests for Chat Attachments Access API handler.

Focuses on:
- Download URL generation endpoint
- expires_minutes parameter validation (1-120)
- Attachment ownership validation
- Attachment status validation (must be COMPLETED)
"""

from uuid import uuid4

from src.domain.models.organisation_tables.chat_message_models import AttachmentStatus

# =============================================================================
# GET /chats/attachment/{attachment_id}/download - Download URL Tests
# =============================================================================


def test_download_success(access_client):
    """Test successful download URL generation for completed attachment."""
    client = access_client["client"]
    service = access_client["service"]

    # Add a completed attachment
    attachment = service.add_completed_attachment(
        filename="test_document.pdf",
        mimetype="application/pdf",
    )

    resp = client.get(f"/chats/attachment/{attachment.attachment_id}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["attachment_id"] == str(attachment.attachment_id)
    assert body["data"]["download_url"] is not None
    assert "https://" in body["data"]["download_url"]
    assert body["data"]["expires_in_seconds"] == 30 * 60  # Default 30 minutes


def test_download_with_custom_expires(access_client):
    """Test download URL generation with custom expiry time."""
    client = access_client["client"]
    service = access_client["service"]

    attachment = service.add_completed_attachment(filename="custom_expires.pdf")

    resp = client.get(
        f"/chats/attachment/{attachment.attachment_id}/download",
        params={"expires_minutes": 60},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["expires_in_seconds"] == 60 * 60  # 60 minutes


def test_download_not_found(access_client):
    """Test download URL for non-existent attachment returns error.

    Note: API returns HTTP 200 with status=ERROR in body (design pattern).
    """
    client = access_client["client"]
    non_existent_id = uuid4()

    resp = client.get(f"/chats/attachment/{non_existent_id}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower() or "access denied" in body["message"].lower()


def test_download_attachment_not_completed(access_client):
    """Test download URL for pending attachment returns error.

    Attachments must be in COMPLETED status to generate download URLs.
    """
    client = access_client["client"]
    service = access_client["service"]
    user_ids = access_client["user_ids"]

    # Add a pending attachment (not completed)
    from tests.api.routers.mocks import FakeAttachment

    pending_attachment = FakeAttachment(
        attachment_id=uuid4(),
        user_id=user_ids["user_id"],
        org_id=user_ids["organisation_id"],
        filename="pending.pdf",
        mimetype="application/pdf",
        size_bytes=1024,
    )
    pending_attachment.status = AttachmentStatus.PENDING_UPLOAD
    service._store[pending_attachment.attachment_id] = pending_attachment

    resp = client.get(f"/chats/attachment/{pending_attachment.attachment_id}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not ready" in body["message"].lower()


def test_download_expires_min_boundary(access_client):
    """Test expires_minutes=1 (minimum) works correctly."""
    client = access_client["client"]
    service = access_client["service"]

    attachment = service.add_completed_attachment(filename="min_expires.pdf")

    resp = client.get(
        f"/chats/attachment/{attachment.attachment_id}/download",
        params={"expires_minutes": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["expires_in_seconds"] == 60  # 1 minute


def test_download_expires_max_boundary(access_client):
    """Test expires_minutes=120 (maximum) works correctly."""
    client = access_client["client"]
    service = access_client["service"]

    attachment = service.add_completed_attachment(filename="max_expires.pdf")

    resp = client.get(
        f"/chats/attachment/{attachment.attachment_id}/download",
        params={"expires_minutes": 120},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["expires_in_seconds"] == 120 * 60  # 120 minutes


def test_download_expires_below_min(access_client):
    """Test expires_minutes=0 (below minimum) returns 422."""
    client = access_client["client"]
    service = access_client["service"]

    attachment = service.add_completed_attachment(filename="below_min.pdf")

    resp = client.get(
        f"/chats/attachment/{attachment.attachment_id}/download",
        params={"expires_minutes": 0},
    )
    assert resp.status_code == 422, resp.text


def test_download_expires_above_max(access_client):
    """Test expires_minutes=121 (above maximum) returns 422."""
    client = access_client["client"]
    service = access_client["service"]

    attachment = service.add_completed_attachment(filename="above_max.pdf")

    resp = client.get(
        f"/chats/attachment/{attachment.attachment_id}/download",
        params={"expires_minutes": 121},
    )
    assert resp.status_code == 422, resp.text


def test_download_invalid_uuid(access_client):
    """Test download URL with invalid UUID format returns 422."""
    client = access_client["client"]

    resp = client.get("/chats/attachment/not-a-uuid/download")
    assert resp.status_code == 422, resp.text


def test_download_url_contains_storage_path(access_client):
    """Test download URL contains the attachment storage path."""
    client = access_client["client"]
    service = access_client["service"]

    attachment = service.add_completed_attachment(filename="storage_path_test.pdf")

    resp = client.get(f"/chats/attachment/{attachment.attachment_id}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    # The fake URL should contain the storage path
    assert attachment.storage_path in body["data"]["download_url"]
