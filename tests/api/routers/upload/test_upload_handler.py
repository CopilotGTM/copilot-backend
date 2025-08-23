import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from uuid import uuid4, UUID
from unittest.mock import AsyncMock, MagicMock

from src.api.dependencies.organisation_dependencies import get_current_user_ids, UserIDs
from src.tasks.attachments_post_upload import process_image
from src.api.fastapi_app import app

# A sample 1x1 transparent PNG for testing
TEST_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"


@pytest.fixture(autouse=True)
def override_user_dependency(test_user_ids):
    """Fixture to override the user dependency for all tests in this module."""

    def _override():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
        )

    app.dependency_overrides[get_current_user_ids] = _override
    yield
    app.dependency_overrides = {}


def test_full_upload_and_processing_flow(
    client: TestClient, db_session: Session, mocker, test_user_ids
):
    """
    Integration test for the entire file upload and processing flow.
    Mocks out Azure Blob Storage and Celery to test the application logic.
    """
    # 1. Mock external services
    mock_storage = mocker.patch(
        "src.service.upload_service.AzureOrganisationBlobStorage", autospec=True
    )
    mock_storage_instance = mock_storage.return_value
    mock_storage_instance.generate_write_sas = AsyncMock(
        return_value="https://fake.blob.core.windows.net/sas-write-url"
    )
    mock_storage_instance.get_blob_properties = AsyncMock(
        return_value={"size": 1234}
    )

    # Mock the celery task sending
    mock_send_task = mocker.patch("src.celery_app.celery.send_task")

    # 2. Call the /presign endpoint
    presign_payload = {
        "filename": "test_image.png",
        "mimetype": "image/png",
        "size_bytes": 1234,
    }
    response = client.post("/upload/presign", json=presign_payload)
    assert response.status_code == 200
    presign_data = response.json()["data"]
    attachment_id = presign_data["attachment_id"]
    assert "upload_url" in presign_data

    # 3. Call the /finalize endpoint
    finalize_payload = {"attachment_id": attachment_id}
    response = client.post("/upload/finalize", json=finalize_payload)
    assert response.status_code == 200

    # 4. Assert that the Celery task was called
    mock_send_task.assert_called_once_with(
        "attachments.process_image",
        args=[
            str(test_user_ids["organisation_id"]),
            attachment_id,
        ],
    )

    # 5. Simulate the Celery worker executing the task
    # Mock the storage interactions within the task itself
    mocker.patch(
        "src.tasks.image_processing._stream_checksum_and_optionally_buffer_image",
        return_value=("fake_checksum", TEST_IMAGE_BYTES),
    )
    mock_upload_blob = mocker.patch("src.tasks.image_processing._upload_blob")

    # Run the task synchronously
    process_image(
        organisation_id=str(test_user_ids["organisation_id"]),
        attachment_id=attachment_id,
    )

    # Assert thumbnails were "uploaded"
    assert mock_upload_blob.call_count == 2  # small and medium

    # 6. Poll the status endpoint to verify completion
    response = client.get(f"/upload/attachment/{attachment_id}")
    assert response.status_code == 200
    status_data = response.json()["data"]

    assert status_data["status"] == "processed"
    assert status_data["checksum"] == "fake_checksum"
    assert status_data["thumbnail_small_url"] is not None
    assert status_data["thumbnail_medium_url"] is not None