"""Chat Attachment Upload Handler Tests

Covers presign validation, finalize lifecycle, get-status polling, and delete.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from src.api.schemas.upload_models import ALLOWED_MIMETYPES, MAX_FILE_SIZE_BYTES
from src.domain.models.organisation_tables.chat_message_models import AttachmentStatus

from tests.api.routers.mocks import FakeUploadService

FAKE_BLOB_STORAGE_URL_PREFIX = "https://fake.blob.core.windows.net/"


def _choose_valid_mimetype() -> str:
    preferred = [
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/msword",
    ]
    for mt in preferred:
        if mt in ALLOWED_MIMETYPES:
            return mt
    return next(iter(ALLOWED_MIMETYPES))


# ---------------------------------------------------------------------------
# Presign happy-path (retained from original)
# ---------------------------------------------------------------------------


def test_presign_succeeds(client: TestClient, dependency_overrides):
    payload = {
        "filename": "example_document.bin",
        "mimetype": _choose_valid_mimetype(),
        "size_bytes": 1234,
    }
    response = client.post("/users/chats/upload/presign", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["success"] is True
    assert data["status"] == AttachmentStatus.PENDING_UPLOAD.value
    assert data["upload_url"].startswith(FAKE_BLOB_STORAGE_URL_PREFIX)
    assert data["filename"] == payload["filename"]
    assert data["mimetype"] == payload["mimetype"]
    for key in ["attachment_id", "expires_in_seconds"]:
        assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Presign validation (schema-level rejections → 422)
# ---------------------------------------------------------------------------


def test_presign_rejects_invalid_mimetype(client: TestClient, dependency_overrides):
    payload = {"filename": "video.mp4", "mimetype": "video/mp4", "size_bytes": 1234}
    resp = client.post("/users/chats/upload/presign", json=payload)
    assert resp.status_code == 422, resp.text


def test_presign_rejects_oversized_file(client: TestClient, dependency_overrides):
    payload = {
        "filename": "huge.pdf",
        "mimetype": _choose_valid_mimetype(),
        "size_bytes": MAX_FILE_SIZE_BYTES + 1,
    }
    resp = client.post("/users/chats/upload/presign", json=payload)
    assert resp.status_code == 422, resp.text


def test_presign_rejects_zero_size(client: TestClient, dependency_overrides):
    payload = {
        "filename": "empty.pdf",
        "mimetype": _choose_valid_mimetype(),
        "size_bytes": 0,
    }
    resp = client.post("/users/chats/upload/presign", json=payload)
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Finalize lifecycle
# ---------------------------------------------------------------------------


def test_finalize_upload_success(
    client: TestClient, dependency_overrides: FakeUploadService, mocker
):
    mocker.patch(
        "src.api.routers.chat_attachments.upload_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    # Step 1: presign
    presign_resp = client.post(
        "/users/chats/upload/presign",
        json={
            "filename": "report.pdf",
            "mimetype": _choose_valid_mimetype(),
            "size_bytes": 2048,
        },
    )
    assert presign_resp.status_code == 200
    attachment_id = presign_resp.json()["data"]["attachment_id"]

    # Step 2: finalize
    finalize_resp = client.post(
        "/users/chats/upload/finalize",
        json={"attachment_id": attachment_id},
    )
    assert finalize_resp.status_code == 200, finalize_resp.text
    data = finalize_resp.json()["data"]
    assert data["status"] == AttachmentStatus.PROCESSING.value


def test_finalize_unknown_attachment_returns_error(
    client: TestClient, dependency_overrides, mocker
):
    mocker.patch(
        "src.api.routers.chat_attachments.upload_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    resp = client.post(
        "/users/chats/upload/finalize",
        json={"attachment_id": str(uuid4())},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# ---------------------------------------------------------------------------
# Get attachment status
# ---------------------------------------------------------------------------


def test_get_attachment_status_after_presign(
    client: TestClient, dependency_overrides: FakeUploadService
):
    presign_resp = client.post(
        "/users/chats/upload/presign",
        json={
            "filename": "deck.pptx",
            "mimetype": _choose_valid_mimetype(),
            "size_bytes": 4096,
        },
    )
    assert presign_resp.status_code == 200
    attachment_id = presign_resp.json()["data"]["attachment_id"]

    status_resp = client.get(f"/users/chats/upload/attachment/{attachment_id}")
    assert status_resp.status_code == 200, status_resp.text
    data = status_resp.json()["data"]
    assert data["status"] == AttachmentStatus.PENDING_UPLOAD.value


def test_get_attachment_status_after_finalize(
    client: TestClient, dependency_overrides: FakeUploadService, mocker
):
    mocker.patch(
        "src.api.routers.chat_attachments.upload_handler.send_chat_task_async",
        return_value="mock-task-id",
    )

    presign_resp = client.post(
        "/users/chats/upload/presign",
        json={
            "filename": "notes.txt",
            "mimetype": _choose_valid_mimetype(),
            "size_bytes": 512,
        },
    )
    assert presign_resp.status_code == 200
    attachment_id = presign_resp.json()["data"]["attachment_id"]

    client.post("/users/chats/upload/finalize", json={"attachment_id": attachment_id})

    status_resp = client.get(f"/users/chats/upload/attachment/{attachment_id}")
    assert status_resp.status_code == 200, status_resp.text
    data = status_resp.json()["data"]
    assert data["status"] == AttachmentStatus.PROCESSING.value


def test_get_attachment_status_not_found(client: TestClient, dependency_overrides):
    resp = client.get(f"/users/chats/upload/attachment/{uuid4()}")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# ---------------------------------------------------------------------------
# Delete attachment
# ---------------------------------------------------------------------------


def test_delete_attachment_success(client: TestClient, dependency_overrides: FakeUploadService):
    presign_resp = client.post(
        "/users/chats/upload/presign",
        json={
            "filename": "draft.docx",
            "mimetype": _choose_valid_mimetype(),
            "size_bytes": 1024,
        },
    )
    assert presign_resp.status_code == 200
    attachment_id = presign_resp.json()["data"]["attachment_id"]

    del_resp = client.delete(f"/users/chats/upload/attachment/{attachment_id}")
    assert del_resp.status_code == 200, del_resp.text
    body = del_resp.json()
    assert body["message"] == "Attachment deleted successfully"


def test_delete_attachment_not_found(client: TestClient, dependency_overrides):
    resp = client.delete(f"/users/chats/upload/attachment/{uuid4()}")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
