"""Organisation-admin catalogue for attachments already sent in chat messages.

The chat attachment router calls this service after enforcing the application
admin role. Queries remain organisation-scoped, and playback URLs are short-lived
so the catalogue never exposes durable blob credentials.
"""

from numbers import Real
from typing import Any, Literal
from uuid import UUID

from pydantic import HttpUrl
from sqlalchemy import exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.api.schemas.upload_models import (
    AttachmentUploaderResponse,
    ChatAttachmentResponse,
)
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
    ChatMessageAttachmentLink,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.cursor import decode_cursor, encode_cursor


class ChatAttachmentCatalogService:
    """List one organisation's sent attachments with playback metadata."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
    ) -> None:
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.storage = AzureOrganisationBlobStorage(organisation_id)

    async def list_attachments(
        self,
        *,
        media_type: Literal["audio"],
        limit: int,
        cursor: str | None,
    ) -> tuple[list[ChatAttachmentResponse], str | None]:
        """Return sent audio newest-first without inspecting the underlying blobs.

        Duration is copied only when an ingest path already persisted it in the
        attachment metadata. Missing duration remains null rather than adding
        synchronous media probing to this read endpoint.
        """
        sent_link_exists = exists().where(
            col(ChatMessageAttachmentLink.attachment_id) == col(Attachment.attachment_id)
        )
        statement = (
            select(Attachment)
            .where(
                col(Attachment.organisation_id) == self.organisation_id,
                col(Attachment.is_deleted).is_(False),
                col(Attachment.status) == AttachmentStatus.COMPLETED,
                col(Attachment.storage_path) != "",
                sent_link_exists,
            )
            .order_by(col(Attachment.created_at).desc(), col(Attachment.attachment_id).desc())
            .limit(limit + 1)
        )
        if media_type == "audio":
            statement = statement.where(col(Attachment.mimetype).ilike("audio/%"))
        if cursor:
            cursor_time, cursor_id = decode_cursor(cursor)
            statement = statement.where(
                (col(Attachment.created_at) < cursor_time)
                | (
                    (col(Attachment.created_at) == cursor_time)
                    & (col(Attachment.attachment_id) < cursor_id)
                )
            )

        result = await self.org_session.execute(statement)
        rows = list(result.scalars().all())
        has_more = len(rows) > limit
        attachments = rows[:limit]
        users = await self._load_uploaders({attachment.user_id for attachment in attachments})

        items: list[ChatAttachmentResponse] = []
        for attachment in attachments:
            user = users.get(attachment.user_id)
            playback_url = await self.storage.generate_read_sas(
                attachment.storage_path,
                minutes=30,
                content_type=attachment.mimetype,
            )
            items.append(
                ChatAttachmentResponse(
                    attachment_id=attachment.attachment_id,
                    filename=attachment.filename,
                    mimetype=attachment.mimetype,
                    size_bytes=attachment.size_bytes,
                    duration_seconds=_persisted_duration_seconds(attachment.attachment_metadata),
                    source_provider=attachment.source_provider,
                    created_at=attachment.created_at,
                    uploaded_by=(
                        AttachmentUploaderResponse(
                            user_id=user.user_id,
                            name=f"{user.first_name} {user.last_name}".strip() or None,
                            email=str(user.email),
                        )
                        if user
                        else None
                    ),
                    playback_url=HttpUrl(playback_url),
                )
            )

        next_cursor = (
            encode_cursor(attachments[-1].created_at, attachments[-1].attachment_id)
            if has_more
            else None
        )
        return items, next_cursor

    async def _load_uploaders(self, user_ids: set[UUID]) -> dict[UUID, User]:
        """Load uploader display data while preserving the organisation boundary."""
        if not user_ids:
            return {}
        # User IDs are externally referenced by tenant rows, so organisation_id
        # remains the trust boundary for this master-database lookup.
        statement = select(User).where(
            col(User.organisation_id) == self.organisation_id,
            col(User.user_id).in_(user_ids),
        )
        result = await self.master_session.execute(statement)
        return {user.user_id: user for user in result.scalars().all()}

    async def close(self) -> None:
        """Release the cached Azure clients created for playback URL generation."""
        await self.storage.close()


def _persisted_duration_seconds(metadata: dict[str, Any] | None) -> float | None:
    """Read a provider-supplied duration without deriving it from file contents."""
    metadata = metadata or {}
    provider_payload = metadata.get("provider_payload")
    candidates = [metadata.get("duration_seconds"), metadata.get("duration")]
    if isinstance(provider_payload, dict):
        candidates.extend(
            [provider_payload.get("duration_seconds"), provider_payload.get("duration")]
        )
    for value in candidates:
        if isinstance(value, Real) and not isinstance(value, bool):
            duration = float(value)
            if duration >= 0:
                return duration
    return None


__all__ = ["ChatAttachmentCatalogService"]
