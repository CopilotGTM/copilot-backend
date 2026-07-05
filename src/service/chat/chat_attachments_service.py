from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.upload_models import (
    AttachmentBatchFinalizeRequest,
    AttachmentBatchPresignRequest,
    FinalizeRequest,
    PresignRequest,
)
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
    ChatMessageAttachmentLink,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

ATTACHMENT_ACCESS_DENIED_MSG = "Attachment not found or access denied"


class UploadServiceError(Exception):
    """Custom exception for UploadService errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class UploadService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID, user_id: UUID):
        """Service handling chat attachment uploads using AsyncSession.

        Note: Organisation search_path/schema is already set by the async provider
        (enforced by get_org_async_session). This class must not change search_path.
        """
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.storage = AzureOrganisationBlobStorage(self.organisation_id)

    async def presign_upload(
        self, payload: PresignRequest, expiry_minutes: int = 15
    ) -> tuple[Attachment, str]:
        """
        Create a preliminary attachment record and generate a presigned URL for uploading.
        Static validation (size, mimetype) is handled by the Pydantic model at the API boundary.
        """
        # Pre-generate the attachment ID so it can be used in the blob path.
        attachment_id = uuid4()
        blob_path = self.storage.plan_blob_path(
            user_id=self.user_id,
            attachment_id=attachment_id,
            original_filename=payload.filename,
        )

        try:
            # Container now per‑org; path retains logical grouping
            upload_url = await self.storage.generate_write_sas(blob_path, minutes=expiry_minutes)
        except Exception as e:
            logger.error(
                "Failed generating write SAS for user %s: %s",
                self.user_id,
                e,
                exc_info=True,
            )
            raise UploadServiceError("Failed to generate upload URL") from e

        attachment = Attachment(
            attachment_id=attachment_id,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            filename=payload.filename,
            storage_path=blob_path,
            mimetype=payload.mimetype,
            size_bytes=payload.size_bytes,
            virus_scan_status="pending",
            status=AttachmentStatus.PENDING_UPLOAD,
            attachment_metadata={"client_reported_size": payload.size_bytes},
        )

        try:
            self.org_session.add(attachment)
            await self.org_session.flush()
            await self.org_session.commit()
            await self.org_session.refresh(attachment)
            logger.info(
                "Created attachment record %s for user %s",
                attachment.attachment_id,
                self.user_id,
            )
            return attachment, upload_url
        except Exception as e:
            await self.org_session.rollback()
            logger.error(
                "Failed to create attachment record for user %s, rolling back: %s",
                self.user_id,
                e,
                exc_info=True,
            )
            raise UploadServiceError("Failed to create attachment record") from e

    async def finalize_upload(self, payload: FinalizeRequest) -> Attachment:
        """Finalize the upload by verifying blob properties and updating the attachment record. Checksum will be calculated asynchronously."""
        attachment: Attachment | None = await self.org_session.get(
            Attachment, payload.attachment_id
        )

        # Check ownership
        if (
            not attachment
            or attachment.organisation_id != self.organisation_id
            or attachment.user_id != self.user_id
        ):
            raise UploadServiceError(ATTACHMENT_ACCESS_DENIED_MSG, status_code=404)

        # Check processing status
        if attachment.status != AttachmentStatus.PENDING_UPLOAD:
            raise UploadServiceError(
                "Attachment is not in a valid state for finalization", status_code=400
            )

        try:
            props = await self.storage.get_blob_properties(attachment.storage_path)

            if not props:
                raise UploadServiceError("Uploaded file not found in storage.", status_code=404)

            actual_size = props.get("size")
            if actual_size is None:
                raise UploadServiceError(
                    "Could not determine file size from storage.", status_code=500
                )

            # Mark as finalized, checksum will be set by async task
            attachment.attachment_metadata["finalized"] = True
            attachment.attachment_metadata["finalized_at"] = datetime.now(UTC).isoformat()
            attachment.status = AttachmentStatus.PROCESSING

            self.org_session.add(attachment)
            await self.org_session.commit()
            await self.org_session.refresh(attachment)
            logger.info(
                "Finalized attachment %s for user %s (checksum pending async)",
                attachment.attachment_id,
                self.user_id,
            )
            return attachment
        except Exception as e:
            await self.org_session.rollback()
            logger.error(
                "Failed to finalize attachment %s, rolling back: %s",
                payload.attachment_id,
                e,
                exc_info=True,
            )
            raise UploadServiceError("Failed to finalize attachment record") from e

    async def get_attachment_details(self, attachment_id: UUID) -> Attachment:
        """Retrieve attachment details by ID, ensuring user has access."""
        attachment: Attachment | None = await self.org_session.get(Attachment, attachment_id)
        if (
            not attachment
            or attachment.organisation_id != self.organisation_id
            or attachment.user_id != self.user_id
        ):
            raise UploadServiceError(ATTACHMENT_ACCESS_DENIED_MSG, status_code=404)

        # The session object tracks the attachment's state. If a Celery task
        # updated it in the background, the main application's session might have
        # a stale view. Refreshing ensures we get the latest data from the DB.
        await self.org_session.refresh(attachment)

        return attachment

    async def generate_download_url(
        self,
        attachment_id: UUID,
        minutes: int = 30,
        download_filename: str | None = None,
    ) -> str:
        """Generate a short‑lived read SAS URL for an attachment ensuring ownership."""
        attachment: Attachment | None = await self.org_session.get(Attachment, attachment_id)

        # Check ownership and status
        if (
            not attachment
            or attachment.organisation_id != self.organisation_id
            or attachment.user_id != self.user_id
        ):
            raise UploadServiceError(ATTACHMENT_ACCESS_DENIED_MSG, status_code=404)

        # Check status
        if attachment.status != AttachmentStatus.COMPLETED:
            raise UploadServiceError("Attachment is not ready", status_code=400)

        # Check storage path
        if not attachment.storage_path:
            raise UploadServiceError("Attachment has no storage path", status_code=400)

        try:
            return await self.storage.generate_read_sas(
                attachment.storage_path,
                minutes=minutes,
                download_filename=download_filename or attachment.filename,
            )
        except Exception as e:  # pragma: no cover - network/azure path
            logger.error(
                "Failed generating read SAS for attachment %s: %s",
                attachment_id,
                e,
                exc_info=True,
            )
            raise UploadServiceError("Could not generate download URL") from e

    async def presign_batch(self, payload: AttachmentBatchPresignRequest, expiry_minutes: int = 15):
        """Batch variant of presign: returns ([(Attachment, url)], [error_dict])."""
        successes: list[tuple[Attachment, str]] = []
        errors: list[dict] = []
        for idx, file in enumerate(payload.files):
            try:
                single_attachment, upload_url = await self.presign_upload(
                    file, expiry_minutes=expiry_minutes
                )
                successes.append((single_attachment, upload_url))
            except UploadServiceError as e:
                errors.append(
                    {
                        "index": idx,
                        "filename": file.filename,
                        "reason": "upload_service_error",
                        "error_message": e.message,
                    }
                )
            except Exception as exc:
                errors.append(
                    {
                        "index": idx,
                        "filename": file.filename,
                        "reason": "unexpected_error",
                        "error_message": str(exc),
                    }
                )
        return successes, errors

    async def finalize_batch(self, payload: AttachmentBatchFinalizeRequest):
        """Batch finalize attachments; returns (results_list, errors_list)."""
        results: list[Attachment] = []
        errors: list[dict] = []
        for item in payload.files:
            try:
                att = await self.finalize_upload(item)
                results.append(att)
            except UploadServiceError as e:
                errors.append(
                    {
                        "attachment_id": str(item.attachment_id),
                        "reason": e.message,
                    }
                )
            except Exception:
                errors.append(
                    {
                        "attachment_id": str(item.attachment_id),
                        "reason": "unexpected_error",
                    }
                )
        return results, errors

    async def delete_attachment(self, attachment_id: UUID) -> bool:
        """Delete an attachment that hasn't been sent in a message yet.

        This performs a soft delete on the attachment record and removes
        the blob (and thumbnails) from Azure storage.

        Args:
            attachment_id: The UUID of the attachment to delete

        Returns:
            True if deletion was successful

        Raises:
            UploadServiceError: If attachment not found, access denied, or already sent
        """
        attachment: Attachment | None = await self.org_session.get(Attachment, attachment_id)

        # Check ownership
        if (
            not attachment
            or attachment.organisation_id != self.organisation_id
            or attachment.user_id != self.user_id
        ):
            raise UploadServiceError(ATTACHMENT_ACCESS_DENIED_MSG, status_code=404)

        # Check if already deleted
        if attachment.is_deleted:
            raise UploadServiceError("Attachment has already been deleted", status_code=400)

        # Check if attachment is linked to any message (can't delete if sent)
        # Use explicit query to avoid lazy-load issues in async context
        link_count_stmt = select(func.count()).where(
            col(ChatMessageAttachmentLink.attachment_id) == attachment_id
        )
        link_count_result = await self.org_session.execute(link_count_stmt)
        link_count = link_count_result.scalar() or 0

        if link_count > 0:
            raise UploadServiceError(
                "Cannot delete attachment that has been sent in a message", status_code=400
            )

        try:
            # Delete blobs from Azure storage (main file + thumbnails)
            blobs_to_delete = [attachment.storage_path]
            if attachment.thumbnail_small_path:
                blobs_to_delete.append(attachment.thumbnail_small_path)
            if attachment.thumbnail_medium_path:
                blobs_to_delete.append(attachment.thumbnail_medium_path)

            for blob_path in blobs_to_delete:
                try:
                    await self.storage.delete_blob(blob_path)
                except Exception as e:
                    # Log but don't fail - blob may already be deleted
                    logger.warning(
                        "Failed to delete blob %s for attachment %s: %s",
                        blob_path,
                        attachment_id,
                        e,
                    )

            # Soft delete the attachment record
            attachment.is_deleted = True
            # Use naive datetime for TIMESTAMP WITHOUT TIME ZONE column
            attachment.deleted_at = datetime.now(UTC).replace(tzinfo=None)

            self.org_session.add(attachment)
            await self.org_session.commit()

            logger.info(
                "Deleted attachment %s for user %s",
                attachment_id,
                self.user_id,
            )
            return True

        except UploadServiceError:
            raise
        except Exception as e:
            await self.org_session.rollback()
            logger.error(
                "Failed to delete attachment %s: %s",
                attachment_id,
                e,
                exc_info=True,
            )
            raise UploadServiceError("Failed to delete attachment") from e

    async def close(self):  # pragma: no cover - simple resource cleanup
        """Release any async resources held by the service (e.g. Azure clients)."""
        try:
            await self.storage.close()
        except Exception as exc:  # Swallow to avoid masking original response errors
            logger.debug("UploadService.close swallow error: %s", exc)
