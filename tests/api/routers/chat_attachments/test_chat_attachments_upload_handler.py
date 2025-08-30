"""Chat Attachment Upload Handler Tests (Reduced)

This module intentionally retains ONLY ONE illustrative test to serve as a
template / reference for writing further tests. All other test cases have been
removed per request to simplify the file.

Retained Test: test_presign_succeeds
Why this one? It exercises the very first step of the attachment lifecycle
(presign) and validates the happy‑path response schema, including:
 - HTTP status 200
 - Presence and format of the generated upload URL
 - Correct initial attachment status (pending_upload)
 - success flag in response payload

Developers can replicate this pattern for finalize, polling, batch, and error
scenarios. Additional recommended future test stubs:
 - test_finalize_succeeds_and_dispatches_task
 - test_presign_rejects_invalid_mimetype
 - test_finalize_invalid_attachment_returns_404
 - test_batch_presign_mixed_success
 - test_finalize_twice_rejected
 - test_download_requires_completed_status
"""

import logging
from fastapi.testclient import TestClient

from src.domain.models.organisation_tables.chat_message_models import AttachmentStatus
from src.api.schemas.upload_models import ALLOWED_MIMETYPES

logger = logging.getLogger("test.chat_attachments.presign")

# Constant prefix asserted to ensure URL shape comes from our (fake) storage layer override.
FAKE_BLOB_STORAGE_URL_PREFIX = "https://fake.blob.core.windows.net/"


def _choose_valid_mimetype() -> str:
    """Select a valid mimetype from config for the presign request.

    Preference order is deterministic for readability; falls back to any allowed type.
    """
    preferred = [
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ]
    for mt in preferred:
        if mt in ALLOWED_MIMETYPES:
            return mt
    return next(iter(ALLOWED_MIMETYPES))


def test_presign_succeeds(client: TestClient, dependency_overrides):
    """Happy path: a valid presign request returns an upload URL and correct initial status.

    GIVEN a JSON body with an allowed mimetype and positive size
    WHEN  POST /users/chats/upload/presign
    THEN 200 OK with:
         - success flag true
         - status == pending_upload
         - upload_url starts with expected blob base
    """
    payload = {
        "filename": "example_document.bin",
        "mimetype": _choose_valid_mimetype(),
        "size_bytes": 1234,
    }
    logger.info("STEP 1: Sending presign request payload=%s", payload)
    response = client.post("/users/chats/upload/presign", json=payload)

    # Basic HTTP success
    logger.info("STEP 2: Received HTTP %s", response.status_code)
    assert response.status_code == 200, response.text

    body = response.json()
    logger.info("STEP 3: Raw response body=%s", body)
    assert "data" in body, "Response should contain 'data' envelope"
    data = body["data"]

    # Core shape / semantic assertions
    logger.info("STEP 4: Validating success/status/url fields")
    assert data["success"] is True
    assert data["status"] == AttachmentStatus.PENDING_UPLOAD.value
    assert data["upload_url"].startswith(FAKE_BLOB_STORAGE_URL_PREFIX)

    # Spot‑check a few expected fields exist (not exhaustively validating schema)
    logger.info("STEP 5: Checking presence of key fields")
    for key in ["attachment_id", "filename", "mimetype", "expires_in_seconds"]:
        assert key in data, f"Missing key in presign response: {key}"

    # Filename & mimetype echo
    logger.info("STEP 6: Verifying echoed filename & mimetype")
    assert data["filename"] == payload["filename"]
    assert data["mimetype"] == payload["mimetype"]
    logger.info("TEST COMPLETE: Presign happy path validated")
