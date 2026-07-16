"""Secure, channel-neutral reads of attachments stored for a chat conversation.

The agent tool and automatic grounding pipeline both call this module. It owns
the organisation/session authorization boundary, blob access, and bounded
content extraction, while returning plain data with no Agno dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlmodel import select
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
    ChatMessage,
    ChatMessageAttachmentLink,
)
from src.infra.config.configs import chat_file_upload_config
from src.infra.media.audio_transcription import transcribe_audio
from src.infra.media.chat_attachment_text import (
    extract_text_from_bytes,
    is_audio_mimetype,
    is_image_mimetype,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

logger = logging.getLogger(__name__)

_BOT_CONTEXT_PREFIX = "ctx:"
AttachmentKind = Literal["image", "text", "audio_transcript", "unavailable"]
AttachmentErrorCode = Literal["invalid", "not_found", "unavailable"]


class AttachmentReadContext(Protocol):
    """Minimum execution context required to enforce attachment access."""

    organisation_id: UUID
    user_id: UUID | None
    metadata: dict[str, Any]

    def acquire_session(self) -> Any:
        """Return an async context manager for an organisation-scoped session."""


@dataclass(slots=True, frozen=True)
class AttachmentReadResult:
    """Neutral attachment content returned to tools and agent adapters."""

    attachment_id: str
    filename: str = "attachment"
    mimetype: str = ""
    kind: AttachmentKind = "unavailable"
    text: str | None = None
    content_bytes: bytes | None = None
    truncated: bool = False
    error_code: AttachmentErrorCode | None = None
    error: str | None = None


def _context_uuid(context: AttachmentReadContext, key: str) -> UUID | None:
    raw = (context.metadata or {}).get(key)
    if not raw:
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        logger.warning("chat attachment reader: invalid %s=%s", key, raw)
        return None


def _allowed_attachment_ids(context: AttachmentReadContext) -> set[UUID]:
    allowed: set[UUID] = set()
    for raw_id in (context.metadata or {}).get("allowed_attachment_ids") or []:
        try:
            allowed.add(raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id)))
        except (TypeError, ValueError, AttributeError):
            logger.warning("chat attachment reader: invalid allowed attachment id=%s", raw_id)
    return allowed


def _unavailable(
    attachment_id: str,
    message: str,
    *,
    error_code: AttachmentErrorCode,
    filename: str = "attachment",
    mimetype: str = "",
) -> AttachmentReadResult:
    return AttachmentReadResult(
        attachment_id=attachment_id,
        filename=filename,
        mimetype=mimetype,
        error_code=error_code,
        error=message,
    )


async def read_attachment_content(
    attachment_id: str,
    *,
    context: AttachmentReadContext,
    max_chars: int = chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS,
) -> AttachmentReadResult:
    """Read one attachment after enforcing its organisation and conversation scope.

    Current transient uploads must be explicitly allowlisted in the execution
    context. Historical files must be linked to the current persisted session.
    Provider URLs are never trusted as an authorization mechanism.
    """
    session_id = _context_uuid(context, "chat_session_id")
    allowed_ids = _allowed_attachment_ids(context)
    if not session_id and not allowed_ids:
        return _unavailable(
            attachment_id,
            "No chat attachment context is available for this run.",
            error_code="not_found",
        )

    max_chars = max_chars if max_chars > 0 else chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS
    if attachment_id.startswith(_BOT_CONTEXT_PREFIX):
        return await _read_legacy_bot_context(
            attachment_id=attachment_id,
            context=context,
            session_id=session_id,
            max_chars=max_chars,
        )

    try:
        parsed_attachment_id = UUID(attachment_id)
    except (TypeError, ValueError, AttributeError):
        return _unavailable(
            attachment_id,
            "attachment_id must be a valid attachment UUID",
            error_code="invalid",
        )

    async with context.acquire_session() as session:
        attachment = await _load_attachment_for_context(
            session=session,
            context=context,
            session_id=session_id,
            allowed_ids=allowed_ids,
            attachment_id=parsed_attachment_id,
        )
        if not attachment:
            return _unavailable(
                attachment_id,
                "Attachment not found in this conversation",
                error_code="not_found",
            )

    if not attachment.storage_path:
        return _unavailable(
            attachment_id,
            "Attachment storage path unavailable",
            error_code="not_found",
            filename=attachment.filename,
            mimetype=attachment.mimetype or "",
        )

    # The org session is released before blob/extraction work so slow storage or
    # document parsing cannot pin a pooled database connection.
    storage = AzureOrganisationBlobStorage(context.organisation_id)
    try:
        content_bytes = await storage.get_blob_content(attachment.storage_path)
        return await _decode_attachment(
            attachment=attachment,
            content_bytes=content_bytes,
            max_chars=max_chars,
        )
    except Exception:
        # One corrupt or unavailable blob degrades this attachment only. The
        # grounder can continue with the remaining selected files.
        logger.exception("Failed to read chat attachment %s", attachment_id)
        return _unavailable(
            attachment_id,
            "Attachment content unavailable",
            error_code="unavailable",
            filename=attachment.filename,
            mimetype=attachment.mimetype or "",
        )
    finally:
        await storage.close()


async def _decode_attachment(
    *,
    attachment: Attachment,
    content_bytes: bytes,
    max_chars: int,
) -> AttachmentReadResult:
    mimetype = (attachment.mimetype or "").lower()
    common = {
        "attachment_id": str(attachment.attachment_id),
        "filename": attachment.filename,
        "mimetype": attachment.mimetype or "",
    }
    if is_image_mimetype(mimetype):
        return AttachmentReadResult(kind="image", content_bytes=content_bytes, **common)

    if is_audio_mimetype(mimetype):
        extracted = await transcribe_audio(content_bytes, attachment.filename)
        kind: AttachmentKind = "audio_transcript"
    else:
        extracted = extract_text_from_bytes(
            content_bytes,
            mimetype,
            attachment.filename,
            max_chars=max_chars + 1,
        )
        kind = "text"

    if not extracted:
        return AttachmentReadResult(
            kind="unavailable",
            text="No readable text could be extracted from this attachment.",
            **common,
        )

    truncated = len(extracted) > max_chars
    return AttachmentReadResult(
        kind=kind,
        text=extracted[:max_chars],
        truncated=truncated,
        **common,
    )


async def _load_attachment_for_context(
    *,
    session: Any,
    context: AttachmentReadContext,
    session_id: UUID | None,
    allowed_ids: set[UUID],
    attachment_id: UUID,
) -> Attachment | None:
    if attachment_id in allowed_ids:
        # A transient ID is useful only for this run; organisation and user
        # filters remain the immutable tenant boundary.
        stmt = select(Attachment).where(
            Attachment.attachment_id == attachment_id,
            Attachment.organisation_id == context.organisation_id,
            Attachment.is_deleted.is_(False),
            Attachment.status.notin_(
                [AttachmentStatus.PENDING_UPLOAD.value, AttachmentStatus.FAILED.value]
            ),
        )
        if context.user_id:
            stmt = stmt.where(Attachment.user_id == context.user_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    if not session_id:
        return None

    stmt = (
        select(Attachment)
        .join(
            ChatMessageAttachmentLink,
            ChatMessageAttachmentLink.attachment_id == Attachment.attachment_id,
        )
        .join(ChatMessage, ChatMessage.message_id == ChatMessageAttachmentLink.message_id)
        .where(
            Attachment.attachment_id == attachment_id,
            Attachment.organisation_id == context.organisation_id,
            Attachment.is_deleted.is_(False),
            Attachment.status.notin_(
                [AttachmentStatus.PENDING_UPLOAD.value, AttachmentStatus.FAILED.value]
            ),
            ChatMessage.session_id == session_id,
        )
    )
    if context.user_id:
        stmt = stmt.where(Attachment.user_id == context.user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _read_legacy_bot_context(
    *,
    attachment_id: str,
    context: AttachmentReadContext,
    session_id: UUID | None,
    max_chars: int,
) -> AttachmentReadResult:
    if not session_id:
        return _unavailable(
            attachment_id,
            "This conversation has no persisted chat attachment context.",
            error_code="not_found",
        )

    try:
        payload = attachment_id.removeprefix(_BOT_CONTEXT_PREFIX)
        message_id_raw, index_raw = payload.split(":", 1)
        message_id = UUID(message_id_raw)
        index = int(index_raw)
    except (TypeError, ValueError, AttributeError):
        return _unavailable(
            attachment_id,
            "Invalid bot attachment context ID",
            error_code="invalid",
        )

    async with context.acquire_session() as session:
        message = await session.get(ChatMessage, message_id)
        if not message or message.session_id != session_id:
            return _unavailable(
                attachment_id,
                "Attachment context not found in this conversation",
                error_code="not_found",
            )
        contexts = (message.extra or {}).get("attachment_contexts") or []
        if index < 0 or index >= len(contexts):
            return _unavailable(
                attachment_id,
                "Attachment context not found in this conversation",
                error_code="not_found",
            )
        entry = contexts[index]

    content = str(entry.get("content") or "")
    content_type = entry.get("content_type", "text")
    return AttachmentReadResult(
        attachment_id=attachment_id,
        filename=entry.get("filename", "attachment"),
        mimetype=entry.get("mimetype", ""),
        kind="audio_transcript" if content_type == "audio_transcript" else "text",
        text=content[:max_chars],
        truncated=len(content) > max_chars,
    )
