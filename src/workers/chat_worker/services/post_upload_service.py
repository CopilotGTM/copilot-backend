"""Post-upload processing service for chat attachments.

This service handles all business logic for attachment post-upload processing:
- Checksum computation
- Thumbnail generation
- Status updates

IMPORTANT: This service follows the fresh session pattern. Sessions are acquired
only when needed for database operations and released immediately after.
No sessions are passed between methods or held during long-running operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
)
from src.infra.config.configs import thumbnail_config
from src.infra.database.sessions import acquire_org_session
from src.infra.media.thumbnail_generator import generate_thumbnails
from src.infra.messaging.attachment_status_publish import publish_attachment_status
from src.infra.messaging.event_models import AttachmentEventUpdate
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("chat_post_upload_service")


@dataclass
class AttachmentData:
    """Data extracted from attachment record for processing."""

    attachment_id: str
    storage_path: str
    mimetype: str
    checksum: str | None


@dataclass
class ProcessingResult:
    """Result of attachment processing."""

    checksum: str | None
    thumbnail_small_path: str | None
    thumbnail_medium_path: str | None
    failed: bool = False


class PostUploadService:
    """Service for handling attachment post-upload processing.

    This service encapsulates all business logic for post-upload processing:
    - Loading attachment data
    - Computing checksums
    - Generating thumbnails
    - Updating status

    Sessions are acquired using `for session in acquire_org_session()` which
    automatically releases the session when the block exits.
    """

    def __init__(self, organisation_id: str):
        """Initialize the service.

        Args:
            organisation_id: The organisation UUID string
        """
        self.organisation_id = organisation_id
        self.org_uuid = UUID(organisation_id)
        self.storage = AzureOrganisationBlobStorage(organisation_id=self.org_uuid)

    def process(self, attachment_id: str) -> bool:
        """Process an attachment: checksum and thumbnail generation.

        This is the main entry point for post-upload processing.

        Args:
            attachment_id: The attachment UUID string

        Returns:
            True if processing succeeded, False otherwise
        """
        try:
            # Phase 1: Load attachment data (DB operation)
            attachment_data = self._load_attachment_data(attachment_id)
            if not attachment_data:
                return False

            # Phase 2: Process attachment (Azure operations - no DB)
            result = self._process_attachment(attachment_data)

            # Phase 3: Finalize (DB operation)
            self._finalize(attachment_id, result)
            return not result.failed

        except Exception as e:
            logger.error(
                "Attachment post-upload failed org=%s attachment=%s error=%s",
                self.organisation_id,
                attachment_id,
                e,
            )
            self._mark_failed(attachment_id, str(e))
            return False

        finally:
            self._close_storage()

    def _load_attachment_data(self, attachment_id: str) -> AttachmentData | None:
        """Load attachment data from database.

        Session is automatically released when the block exits.
        """
        for session in acquire_org_session(self.org_uuid):
            attachment = session.get(Attachment, attachment_id)
            if not attachment:
                logger.warning("Attachment %s not found for processing", attachment_id)
                return None

            # Check if already processed
            if (
                attachment.status
                in {
                    AttachmentStatus.COMPLETED,
                    AttachmentStatus.FAILED,
                }
                and attachment.checksum
            ):
                logger.info("Attachment %s already processed", attachment_id)
                return None

            return AttachmentData(
                attachment_id=str(attachment.attachment_id),
                storage_path=attachment.storage_path or "",
                mimetype=attachment.mimetype or "",
                checksum=attachment.checksum,
            )

        return None

    def _process_attachment(self, data: AttachmentData) -> ProcessingResult:
        """Process attachment: compute checksum and generate thumbnails.

        This method does NOT use any database sessions.
        All operations are Azure blob storage operations.
        """
        checksum = data.checksum
        thumbnail_small_path = None
        thumbnail_medium_path = None
        failed = False

        # Compute checksum if missing
        try:
            if data.storage_path and not checksum:
                checksum = run_async(self.storage.compute_checksum(data.storage_path))
        except Exception:
            logger.exception("Checksum computation failed for attachment %s", data.attachment_id)

        # Generate thumbnails if supported
        try:
            mt = (data.mimetype or "").lower()
            is_supported = thumbnail_config.is_supported(mt)
            logger.info(
                "Thumbnail check: attachment=%s, mimetype=%s, is_supported=%s",
                data.attachment_id,
                mt,
                is_supported,
            )
            if is_supported:
                created = run_async(
                    generate_thumbnails(
                        storage=self.storage,
                        original_blob_path=data.storage_path,
                        mimetype=data.mimetype,
                    )
                )
                logger.info(
                    "Thumbnail generation result: attachment=%s, created=%s",
                    data.attachment_id,
                    created,
                )
                if created:
                    thumbnail_small_path = created.get("thumb_small")
                    thumbnail_medium_path = created.get("thumb_medium")
        except Exception as e:
            logger.exception("Thumbnail generation failed for attachment %s", data.attachment_id)
            # Don't mark as failed - thumbnails are optional
            logger.warning("Continuing without thumbnails: %s", e)

        return ProcessingResult(
            checksum=checksum,
            thumbnail_small_path=thumbnail_small_path,
            thumbnail_medium_path=thumbnail_medium_path,
            failed=failed,
        )

    def _finalize(self, attachment_id: str, result: ProcessingResult) -> None:
        """Finalize processing: update database and publish status.

        Session is automatically released when the block exits.
        """
        thumb_path: str | None = None
        attachment_data_for_publish: dict | None = None

        for session in acquire_org_session(self.org_uuid):
            attachment = session.get(Attachment, attachment_id)
            if not attachment:
                logger.warning("Attachment %s not found for finalization", attachment_id)
                return

            # Update attachment with processing results
            if result.checksum:
                attachment.checksum = result.checksum
            if result.thumbnail_small_path:
                attachment.thumbnail_small_path = result.thumbnail_small_path
            if result.thumbnail_medium_path:
                attachment.thumbnail_medium_path = result.thumbnail_medium_path

            attachment.status = AttachmentStatus.COMPLETED
            session.merge(attachment)
            session.commit()

            # Store path for SAS URL generation (after session release)
            thumb_path = attachment.thumbnail_small_path

            # Store attachment data for status publish
            attachment_data_for_publish = {
                "attachment_id": str(attachment.attachment_id),
                "status": attachment.status,
                "updated_at": attachment.updated_at.isoformat() if attachment.updated_at else None,
                "processing_error": attachment.processing_error,
            }

            logger.info("Attachment post-upload completed attachment=%s", attachment_id)

        # Generate SAS URL for thumbnail (outside of session)
        if attachment_data_for_publish:
            thumbnail_small_url = self._generate_thumbnail_url(thumb_path)

            logger.info(
                "Publishing attachment status: attachment_id=%s, thumb_path=%s, thumb_url=%s",
                attachment_data_for_publish["attachment_id"],
                thumb_path,
                thumbnail_small_url[:50] if thumbnail_small_url else None,
            )

            # Publish status event - ensure status is string value for JSON serialization
            status_value = attachment_data_for_publish["status"]
            if hasattr(status_value, "value"):
                status_value = status_value.value

            event = AttachmentEventUpdate(
                attachment_id=attachment_data_for_publish["attachment_id"],
                status=status_value,
                updated_at=attachment_data_for_publish["updated_at"],
                error_message=attachment_data_for_publish["processing_error"],
                thumbnail_small_url=thumbnail_small_url,
            )
            publish_attachment_status(self.organisation_id, event)

    def _mark_failed(self, attachment_id: str, error_message: str) -> None:
        """Mark attachment as failed.

        Session is automatically released when the block exits.
        """
        try:
            for session in acquire_org_session(self.org_uuid):
                attachment = session.get(Attachment, attachment_id)
                if not attachment:
                    return

                attachment.status = AttachmentStatus.FAILED
                attachment.processing_error = f"post_upload_failed: {error_message}"[:500]
                session.merge(attachment)
                session.commit()

                # Publish failure status
                event = AttachmentEventUpdate(
                    attachment_id=str(attachment.attachment_id),
                    status=AttachmentStatus.FAILED,
                    updated_at=attachment.updated_at.isoformat() if attachment.updated_at else None,
                    error_message=attachment.processing_error,
                    thumbnail_small_url=None,
                )
                publish_attachment_status(self.organisation_id, event)
        except Exception:
            logger.debug(
                "Failed to persist FAILED state for attachment %s",
                attachment_id,
                exc_info=True,
            )

    def _generate_thumbnail_url(self, path: str | None) -> str | None:
        """Generate SAS URL for thumbnail."""
        if not path:
            return None
        try:
            return run_async(self.storage.generate_read_sas(path, minutes=120))
        except Exception:
            logger.debug("Thumbnail SAS generation failed for path=%s", path, exc_info=True)
            return None

    def _close_storage(self) -> None:
        """Close storage client."""
        try:
            close_coro = self.storage.close()
            if close_coro is not None:
                run_async(close_coro)
        except Exception:
            logger.debug("Failed to close storage client", exc_info=True)
