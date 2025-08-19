from __future__ import annotations

from uuid import UUID, uuid4
from datetime import datetime
from typing import Dict, Any

from sqlmodel import Session, text

from src.infra.utils.logger import get_logger
from src.domain.models.organisation_tables.chat_message_models import Attachment
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.api.schemas.upload_models import PresignRequest, FinalizeRequest
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
            upload_url = await self.storage.generate_write_sas("attachments", blob_path, minutes=15)
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
            size_bytes=payload.size_bytes,  # Store the client-reported size initially
            virus_scan_status="pending",
            processing_status="pending",
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
        attachment = self.org_session.get(Attachment, payload.attachment_id)
        if not attachment or attachment.organisation_id != self.organisation_id or attachment.user_id != user_id:
            raise UploadServiceError("Attachment not found or access denied", status_code=404)

        try:
            props = await self.storage.get_blob_properties("attachments", attachment.storage_path)
            if not props:
                raise UploadServiceError("Uploaded file not found in storage.", status_code=404)

            actual_size = props.get("size")
            if actual_size is None:
                raise UploadServiceError("Could not determine file size from storage.", status_code=500)

            # Service-level validation: compare client-reported size with actual size
            if actual_size != attachment.size_bytes:
                logger.error(
                    "CRITICAL: Size mismatch for attachment %s. Client reported %s, storage has %s. Rejecting.",
                    attachment.attachment_id,
                    attachment.size_bytes,
                    actual_size,
                )
                await self.storage.delete_blob("attachments", attachment.storage_path)
                self.org_session.delete(attachment)
                self.org_session.commit()
                raise UploadServiceError(
                    "File integrity check failed: reported file size does not match actual file size.", status_code=400
                )

            # Mark as finalized, checksum will be set by async task
            attachment.attachment_metadata["finalized"] = True
            attachment.attachment_metadata["finalized_at"] = datetime.utcnow().isoformat()

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
