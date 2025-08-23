from __future__ import annotations

from uuid import UUID, uuid4
from datetime import datetime
from typing import Dict, Any

from sqlmodel import Session, text

from src.infra.utils.logger import get_logger
from src.domain.models.organisation_tables.chat_message_models import Attachment, AttachmentStatus
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.api.schemas.upload_models import (
    PresignRequest,
    FinalizeRequest,
    AttachmentBatchPresignRequest,
    AttachmentBatchFinalizeRequest,
)
from src.infra.utils.resource_naming import get_schema_name

logger = get_logger(__name__)


class UploadServiceError(Exception):
    """Custom exception for UploadService errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class UploadService:
    def __init__(self, org_session: Session, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.storage = AzureOrganisationBlobStorage(self.organisation_id)
        self.schema_name = get_schema_name(self.organisation_id)

    def _set_schema(self):
        self.org_session.execute(text(f"SET search_path TO {self.schema_name}"))

    async def presign_upload(self, payload: PresignRequest, user_id: UUID) -> tuple[Attachment, str]:
        """
        Create a preliminary attachment record and generate a presigned URL for uploading.
        Static validation (size, mimetype) is handled by the Pydantic model at the API boundary.
        """
        self._set_schema()
        # Pre-generate the attachment ID so it can be used in the blob path.
        attachment_id = uuid4()
        blob_path = self.storage.plan_blob_path(
            user_id=user_id, attachment_id=attachment_id, original_filename=payload.filename
        )

        try:
            # Container now per‑org; path retains logical grouping
            upload_url = await self.storage.generate_write_sas(blob_path, minutes=15)
        except Exception as e:
            logger.error("Failed generating write SAS for user %s: %s", user_id, e, exc_info=True)
            raise UploadServiceError("Failed to generate upload URL") from e

        attachment = Attachment(
            attachment_id=attachment_id,
            organisation_id=self.organisation_id,
            user_id=user_id,
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
            self.org_session.commit()
            self.org_session.refresh(attachment)
            logger.info("Created attachment record %s for user %s", attachment.attachment_id, user_id)
            return attachment, upload_url
        except Exception as e:
            self.org_session.rollback()
            logger.error(
                "Failed to create attachment record for user %s, rolling back: %s", user_id, e, exc_info=True
            )
            raise UploadServiceError("Failed to create attachment record") from e

    async def finalize_upload(self, payload: FinalizeRequest, user_id: UUID) -> Attachment:
        """Finalize the upload by verifying blob properties and updating the attachment record. Checksum will be calculated asynchronously."""
        self._set_schema()
        attachment: Attachment = self.org_session.get(Attachment, payload.attachment_id)

        # Check ownership
        if not attachment or attachment.organisation_id != self.organisation_id or attachment.user_id != user_id:
            raise UploadServiceError("Attachment not found or access denied", status_code=404)

        # Check processing status
        if attachment.status != AttachmentStatus.PENDING_UPLOAD:
            raise UploadServiceError("Attachment is not in a valid state for finalization", status_code=400)

        try:
            props = await self.storage.get_blob_properties(attachment.storage_path)

            if not props:
                raise UploadServiceError("Uploaded file not found in storage.", status_code=404)

            actual_size = props.get("size")
            if actual_size is None:
                raise UploadServiceError("Could not determine file size from storage.", status_code=500)

            # Mark as finalized, checksum will be set by async task
            attachment.attachment_metadata["finalized"] = True
            attachment.attachment_metadata["finalized_at"] = datetime.utcnow().isoformat()
            attachment.status = AttachmentStatus.PROCESSING

            self.org_session.add(attachment)
            self.org_session.commit()
            self.org_session.refresh(attachment)
            logger.info("Finalized attachment %s for user %s (checksum pending async)", attachment.attachment_id, user_id)
            return attachment
        except Exception as e:
            self.org_session.rollback()
            logger.error(
                "Failed to finalize attachment %s, rolling back: %s", payload.attachment_id, e, exc_info=True
            )
            raise UploadServiceError("Failed to finalize attachment record") from e

    async def get_attachment_details(self, attachment_id: UUID, user_id: UUID) -> Attachment:
        """Retrieve attachment details by ID, ensuring user has access."""
        self._set_schema()
        attachment = self.org_session.get(Attachment, attachment_id)
        if not attachment or attachment.organisation_id != self.organisation_id or attachment.user_id != user_id:
            raise UploadServiceError("Attachment not found or access denied", status_code=404)
        
        # The session object tracks the attachment's state. If a Celery task
        # updated it in the background, the main application's session might have
        # a stale view. Refreshing ensures we get the latest data from the DB.
        self.org_session.refresh(attachment)
        
        return attachment

    # ---------------- Batch operations ---------------- #

    async def presign_batch(self, payload: AttachmentBatchPresignRequest, user_id: UUID, expiry_minutes: int = 15):
        """Batch variant of presign: returns ([(Attachment, url)], [error_dict])."""
        successes: list[tuple[Attachment, str]] = []
        errors: list[dict] = []
        for idx, file in enumerate(payload.files):
            try:
                single_attachment, upload_url = await self.presign_upload(file, user_id)
                successes.append((single_attachment, upload_url))
            except UploadServiceError as e:
                errors.append({
                    "index": idx,
                    "filename": file.filename,
                    "reason": "upload_service_error",
                    "error_message": e.message,
                })
            except Exception as exc:
                errors.append({
                    "index": idx,
                    "filename": file.filename,
                    "reason": "unexpected_error",
                    "error_message": str(exc),
                })
        return successes, errors

    async def finalize_batch(self, payload: AttachmentBatchFinalizeRequest, user_id: UUID):
        """Batch finalize attachments; returns (results_list, errors_list)."""
        results: list[Attachment] = []
        errors: list[dict] = []
        for item in payload.files:
            try:
                att = await self.finalize_upload(item, user_id)
                results.append(att)
            except UploadServiceError as e:
                errors.append({
                    "attachment_id": str(item.attachment_id),
                    "reason": e.message,
                })
            except Exception:
                errors.append({
                    "attachment_id": str(item.attachment_id),
                    "reason": "unexpected_error",
                })
        return results, errors

    async def close(self):  # pragma: no cover - simple resource cleanup
        """Release any async resources held by the service (e.g. Azure clients)."""
        try:
            await self.storage.close()
        except Exception as exc:  # Swallow to avoid masking original response errors
            logger.debug("UploadService.close swallow error: %s", exc)
