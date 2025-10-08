from datetime import datetime, timezone
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
        self.created_at = datetime.now(timezone.utc)
        self.status = AttachmentStatus.PENDING_UPLOAD


class FakeUploadService:
    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._store: dict[UUID, FakeAttachment] = {}

    async def presign_upload(self, payload, user_id: UUID):
        att_id = uuid4()
        att = FakeAttachment(
            att_id,
            user_id,
            self.organisation_id,
            payload.filename,
            payload.mimetype,
            payload.size_bytes,
        )
        self._store[att_id] = att
        upload_url = (
            f"https://fake.blob.core.windows.net/container/{att.storage_path}?sastoken"
        )
        return att, upload_url

    async def finalize_upload(self, payload, user_id: UUID):
        att = self._store.get(payload.attachment_id)
        if not att or att.user_id != user_id:
            from src.service.chat.chat_attachments_service import UploadServiceError

            raise UploadServiceError(
                "Attachment not found or access denied", status_code=404
            )
        att.status = AttachmentStatus.PROCESSING
        return att

    async def get_attachment_details(self, attachment_id: UUID, user_id: UUID):
        att = self._store.get(attachment_id)
        if not att or att.user_id != user_id:
            from src.service.chat.chat_attachments_service import UploadServiceError

            raise UploadServiceError(
                "Attachment not found or access denied", status_code=404
            )
        return att

    async def close(self):
        return None
