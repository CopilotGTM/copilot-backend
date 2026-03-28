"""Tests for PostUploadService - attachment post-upload processing.

Covers checksum computation, thumbnail generation, status transitions,
and error handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
)
from src.workers.chat_worker.services.post_upload_service import (
    AttachmentData,
    PostUploadService,
    ProcessingResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_attachment(
    *,
    attachment_id=None,
    status=AttachmentStatus.PROCESSING,
    storage_path="attachments/file.pdf",
    mimetype="application/pdf",
    checksum=None,
    thumbnail_small_path=None,
    thumbnail_medium_path=None,
    processing_error=None,
    updated_at=None,
):
    att = MagicMock(spec=Attachment)
    att.attachment_id = attachment_id or uuid4()
    att.status = status
    att.storage_path = storage_path
    att.mimetype = mimetype
    att.checksum = checksum
    att.thumbnail_small_path = thumbnail_small_path
    att.thumbnail_medium_path = thumbnail_medium_path
    att.processing_error = processing_error
    att.updated_at = updated_at
    return att


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadAttachmentData:
    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_returns_data_for_valid_attachment(self, mock_storage_cls, mock_acquire):
        """Valid attachment returns AttachmentData."""
        att = _make_attachment()
        mock_session = MagicMock()
        mock_session.get.return_value = att
        mock_acquire.return_value = iter([mock_session])
        mock_storage_cls.return_value = MagicMock()

        service = PostUploadService(str(uuid4()))
        result = service._load_attachment_data(str(att.attachment_id))

        assert result is not None
        assert result.storage_path == "attachments/file.pdf"
        assert result.mimetype == "application/pdf"

    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_returns_none_for_missing_attachment(self, mock_storage_cls, mock_acquire):
        """Missing attachment → None."""
        mock_session = MagicMock()
        mock_session.get.return_value = None
        mock_acquire.return_value = iter([mock_session])
        mock_storage_cls.return_value = MagicMock()

        service = PostUploadService(str(uuid4()))
        result = service._load_attachment_data(str(uuid4()))
        assert result is None

    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_skips_already_completed_with_checksum(self, mock_storage_cls, mock_acquire):
        """Already COMPLETED attachment with checksum → skip (return None)."""
        att = _make_attachment(status=AttachmentStatus.COMPLETED, checksum="abc123")
        mock_session = MagicMock()
        mock_session.get.return_value = att
        mock_acquire.return_value = iter([mock_session])
        mock_storage_cls.return_value = MagicMock()

        service = PostUploadService(str(uuid4()))
        result = service._load_attachment_data(str(att.attachment_id))
        assert result is None


class TestFinalize:
    @patch("src.workers.chat_worker.services.post_upload_service.publish_attachment_status")
    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_sets_status_completed(self, mock_storage_cls, mock_acquire, mock_publish):
        """Finalize sets status to COMPLETED."""
        att = _make_attachment()
        mock_session = MagicMock()
        mock_session.get.return_value = att
        mock_acquire.return_value = iter([mock_session])

        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage

        service = PostUploadService(str(uuid4()))
        processing_result = ProcessingResult(
            checksum="sha256-abc",
            thumbnail_small_path="thumbs/small.png",
            thumbnail_medium_path="thumbs/medium.png",
        )
        service._finalize(str(att.attachment_id), processing_result)

        assert att.status == AttachmentStatus.COMPLETED
        assert att.checksum == "sha256-abc"
        assert att.thumbnail_small_path == "thumbs/small.png"
        mock_session.commit.assert_called_once()

    @patch("src.workers.chat_worker.services.post_upload_service.publish_attachment_status")
    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_publishes_status_event(self, mock_storage_cls, mock_acquire, mock_publish):
        """Status event published to Redis after finalize."""
        att = _make_attachment()
        mock_session = MagicMock()
        mock_session.get.return_value = att
        mock_acquire.return_value = iter([mock_session])
        mock_storage_cls.return_value = MagicMock()

        service = PostUploadService(str(uuid4()))
        processing_result = ProcessingResult(
            checksum="sha256-abc",
            thumbnail_small_path=None,
            thumbnail_medium_path=None,
        )
        service._finalize(str(att.attachment_id), processing_result)

        mock_publish.assert_called_once()


class TestMarkFailed:
    @patch("src.workers.chat_worker.services.post_upload_service.publish_attachment_status")
    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_sets_status_failed(self, mock_storage_cls, mock_acquire, mock_publish):
        """Mark failed sets status to FAILED with error message."""
        att = _make_attachment()
        mock_session = MagicMock()
        mock_session.get.return_value = att
        mock_acquire.return_value = iter([mock_session])
        mock_storage_cls.return_value = MagicMock()

        service = PostUploadService(str(uuid4()))
        service._mark_failed(str(att.attachment_id), "Checksum failed")

        assert att.status == AttachmentStatus.FAILED
        assert "Checksum failed" in att.processing_error

    @patch("src.workers.chat_worker.services.post_upload_service.publish_attachment_status")
    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_truncates_long_error(self, mock_storage_cls, mock_acquire, mock_publish):
        """Long error messages truncated to 500 chars."""
        att = _make_attachment()
        mock_session = MagicMock()
        mock_session.get.return_value = att
        mock_acquire.return_value = iter([mock_session])
        mock_storage_cls.return_value = MagicMock()

        service = PostUploadService(str(uuid4()))
        long_error = "x" * 1000
        service._mark_failed(str(att.attachment_id), long_error)

        assert len(att.processing_error) <= 500

    @patch("src.workers.chat_worker.services.post_upload_service.acquire_org_session")
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_silently_handles_db_failure(self, mock_storage_cls, mock_acquire):
        """DB error during failure marking → logged, not re-raised."""
        mock_acquire.side_effect = Exception("DB down")
        mock_storage_cls.return_value = MagicMock()

        service = PostUploadService(str(uuid4()))
        # Should not raise
        service._mark_failed(str(uuid4()), "error")


class TestProcessFlow:
    @patch("src.workers.chat_worker.services.post_upload_service.PostUploadService._close_storage")
    @patch("src.workers.chat_worker.services.post_upload_service.PostUploadService._finalize")
    @patch(
        "src.workers.chat_worker.services.post_upload_service.PostUploadService._process_attachment"
    )
    @patch(
        "src.workers.chat_worker.services.post_upload_service.PostUploadService._load_attachment_data"
    )
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_happy_path(self, mock_storage_cls, mock_load, mock_process, mock_finalize, mock_close):
        """Phase 1→2→3 completes successfully."""
        mock_storage_cls.return_value = MagicMock()
        mock_load.return_value = AttachmentData(
            attachment_id=str(uuid4()),
            storage_path="path",
            mimetype="application/pdf",
            checksum=None,
        )
        mock_process.return_value = ProcessingResult(
            checksum="abc", thumbnail_small_path=None, thumbnail_medium_path=None
        )

        service = PostUploadService(str(uuid4()))
        result = service.process(str(uuid4()))

        assert result is True
        mock_load.assert_called_once()
        mock_process.assert_called_once()
        mock_finalize.assert_called_once()

    @patch("src.workers.chat_worker.services.post_upload_service.PostUploadService._close_storage")
    @patch(
        "src.workers.chat_worker.services.post_upload_service.PostUploadService._load_attachment_data"
    )
    @patch("src.workers.chat_worker.services.post_upload_service.AzureOrganisationBlobStorage")
    def test_load_failure_returns_false(self, mock_storage_cls, mock_load, mock_close):
        """load returns None → returns False."""
        mock_storage_cls.return_value = MagicMock()
        mock_load.return_value = None

        service = PostUploadService(str(uuid4()))
        result = service.process(str(uuid4()))

        assert result is False
