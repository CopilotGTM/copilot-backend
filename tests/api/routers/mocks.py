from datetime import UTC, datetime
from uuid import UUID, uuid4

from src.domain.models.organisation_tables.chat_message_models import AttachmentStatus


class FakeAttachment:
    def __init__(
        self,
        attachment_id: UUID,
        user_id: UUID,
        org_id: UUID,
        filename: str,
        mimetype: str,
        size_bytes: int,
    ):
        self.attachment_id = attachment_id
        self.user_id = user_id
        self.organisation_id = org_id
        self.filename = filename
        self.mimetype = mimetype
        self.size_bytes = size_bytes
        self.storage_path = f"attachments/{user_id}/{attachment_id}/{filename}"
        self.created_at = datetime.now(UTC)
        self.status = AttachmentStatus.PENDING_UPLOAD


class FakeUploadService:
    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._store: dict[UUID, FakeAttachment] = {}

    async def presign_upload(self, payload, expiry_minutes: int = 15):
        """Create attachment record and return presigned URL (matches real service signature)."""
        att_id = uuid4()
        att = FakeAttachment(
            att_id,
            self.user_id,  # Use instance user_id like real service
            self.organisation_id,
            payload.filename,
            payload.mimetype,
            payload.size_bytes,
        )
        self._store[att_id] = att
        upload_url = f"https://fake.blob.core.windows.net/container/{att.storage_path}?sastoken"
        return att, upload_url

    async def finalize_upload(self, payload):
        """Finalize upload (matches real service signature)."""
        att = self._store.get(payload.attachment_id)
        if not att or att.user_id != self.user_id:
            from src.service.chat.chat_attachments_service import UploadServiceError

            raise UploadServiceError("Attachment not found or access denied", status_code=404)
        att.status = AttachmentStatus.PROCESSING
        return att

    async def get_attachment_details(self, attachment_id: UUID):
        """Get attachment details (matches real service signature)."""
        att = self._store.get(attachment_id)
        if not att or att.user_id != self.user_id:
            from src.service.chat.chat_attachments_service import UploadServiceError

            raise UploadServiceError("Attachment not found or access denied", status_code=404)
        return att

    async def generate_download_url(
        self,
        attachment_id: UUID,
        minutes: int = 120,
        download_filename: str | None = None,
    ) -> str:
        """Generate download URL for an attachment."""
        from src.service.chat.chat_attachments_service import UploadServiceError

        att = self._store.get(attachment_id)

        # Check if attachment exists and belongs to the right user
        if not att or att.user_id != self.user_id:
            raise UploadServiceError("Attachment not found or access denied", status_code=404)

        if att.organisation_id != self.organisation_id:
            raise UploadServiceError("Attachment not found or access denied", status_code=404)

        # Check status
        if att.status != AttachmentStatus.COMPLETED:
            raise UploadServiceError("Attachment is not ready", status_code=400)

        # Check storage path
        if not att.storage_path:
            raise UploadServiceError("Attachment has no storage path", status_code=400)

        # Return a fake download URL
        return f"https://fake.blob.core.windows.net/container/{att.storage_path}?sastoken&expires={minutes}m"

    def add_completed_attachment(
        self,
        attachment_id: UUID | None = None,
        filename: str = "test.pdf",
        mimetype: str = "application/pdf",
        size_bytes: int = 1024,
    ) -> FakeAttachment:
        """Add a completed attachment for download testing."""
        att_id = attachment_id or uuid4()
        att = FakeAttachment(
            att_id,
            self.user_id,
            self.organisation_id,
            filename,
            mimetype,
            size_bytes,
        )
        att.status = AttachmentStatus.COMPLETED
        self._store[att_id] = att
        return att

    async def close(self):
        return None
