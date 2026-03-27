"""Tools for accessing chat attachments in the current conversation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from agno.media import Image as AgnoImage
from agno.tools.function import ToolResult as AgnoToolResult
from sqlmodel import select

from src.agents.tools.base import ToolResult, execute_with_capabilities
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

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_BOT_CONTEXT_PREFIX = "ctx:"


def _session_id_from_context(context: AgentContext) -> UUID | None:
    raw = (context.metadata or {}).get("chat_session_id")
    if not raw:
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except Exception:
        logger.warning("chat attachment tools: invalid chat_session_id=%s", raw)
        return None


def _current_message_id_from_context(context: AgentContext) -> UUID | None:
    raw = (context.metadata or {}).get("current_user_message_id")
    if not raw:
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except Exception:
        logger.warning("chat attachment tools: invalid current_user_message_id=%s", raw)
        return None


def _allowed_attachment_ids_from_context(context: AgentContext) -> set[UUID]:
    raw_ids = (context.metadata or {}).get("allowed_attachment_ids") or []
    allowed: set[UUID] = set()
    for raw_id in raw_ids:
        try:
            allowed.add(raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id)))
        except Exception:
            logger.warning("chat attachment tools: invalid allowed attachment id=%s", raw_id)
    return allowed


def _truncate_text(content: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    return content[:max_chars], True


async def list_chat_attachments(
    *,
    context: AgentContext,
    scope: str = "conversation",
    limit: int = 10,
) -> dict[str, Any]:
    """List attachments available in the current chat conversation.

    This is a fallback tool. Prefer reading attachment IDs from the message-level
    attachment manifests already present in conversation history, then call
    `read_chat_attachment` directly.
    """
    session_id = _session_id_from_context(context)
    allowed_ids = _allowed_attachment_ids_from_context(context)
    current_message_id = _current_message_id_from_context(context)
    normalized_scope = scope if scope in {"conversation", "current_message"} else "conversation"

    attachment_entries: list[dict[str, Any]] = []

    async with context.acquire_session() as session:
        if session_id:
            message_stmt = (
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.desc())
            )
            if normalized_scope == "current_message" and current_message_id:
                message_stmt = message_stmt.where(ChatMessage.message_id == current_message_id)
            messages = list((await session.execute(message_stmt)).scalars().all())
            message_map = {message.message_id: message for message in messages}

            if message_map:
                attachment_stmt = (
                    select(ChatMessageAttachmentLink, Attachment)
                    .join(
                        Attachment,
                        ChatMessageAttachmentLink.attachment_id == Attachment.attachment_id,
                    )
                    .where(
                        ChatMessageAttachmentLink.message_id.in_(list(message_map.keys())),
                        Attachment.organisation_id == context.organisation_id,
                        Attachment.is_deleted.is_(False),
                        Attachment.status.notin_(
                            [
                                AttachmentStatus.PENDING_UPLOAD.value,
                                AttachmentStatus.FAILED.value,
                            ]
                        ),
                    )
                    .order_by(ChatMessageAttachmentLink.attachment_order)
                )
                if context.user_id:
                    attachment_stmt = attachment_stmt.where(Attachment.user_id == context.user_id)
                for link, attachment in (await session.execute(attachment_stmt)).all():
                    message = message_map.get(link.message_id)
                    if not message:
                        continue
                    attachment_entries.append(
                        {
                            "attachment_id": str(attachment.attachment_id),
                            "message_id": str(link.message_id),
                            "filename": attachment.filename,
                            "mimetype": attachment.mimetype,
                            "source_provider": (
                                getattr(
                                    attachment.source_provider, "value", attachment.source_provider
                                )
                                if getattr(attachment, "source_provider", None)
                                else None
                            ),
                            "created_at": message.created_at.isoformat()
                            if message.created_at
                            else None,
                        }
                    )

            for message in messages:
                contexts = (message.extra or {}).get("attachment_contexts") or []
                for idx, ctx_entry in enumerate(contexts):
                    attachment_entries.append(
                        {
                            "attachment_id": f"{_BOT_CONTEXT_PREFIX}{message.message_id}:{idx}",
                            "message_id": str(message.message_id),
                            "filename": ctx_entry.get("filename", "attachment"),
                            "mimetype": ctx_entry.get("mimetype", ""),
                            "source_provider": ctx_entry.get("source_provider"),
                            "created_at": message.created_at.isoformat()
                            if message.created_at
                            else None,
                        }
                    )

        if allowed_ids:
            # Current-turn bot attachments may not belong to persisted session history yet,
            # so allow explicit transient IDs for this run only.
            transient_stmt = select(Attachment).where(
                Attachment.attachment_id.in_(list(allowed_ids)),
                Attachment.organisation_id == context.organisation_id,
                Attachment.is_deleted.is_(False),
                Attachment.status.notin_(
                    [AttachmentStatus.PENDING_UPLOAD.value, AttachmentStatus.FAILED.value]
                ),
            )
            if context.user_id:
                transient_stmt = transient_stmt.where(Attachment.user_id == context.user_id)
            for attachment in (await session.execute(transient_stmt)).scalars().all():
                attachment_entries.append(
                    {
                        "attachment_id": str(attachment.attachment_id),
                        "message_id": None,
                        "filename": attachment.filename,
                        "mimetype": attachment.mimetype,
                        "source_provider": (
                            getattr(attachment.source_provider, "value", attachment.source_provider)
                            if getattr(attachment, "source_provider", None)
                            else None
                        ),
                        "created_at": attachment.created_at.isoformat()
                        if attachment.created_at
                        else None,
                    }
                )

    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in attachment_entries:
        attachment_id = str(entry.get("attachment_id") or "")
        if attachment_id in seen_ids:
            continue
        seen_ids.add(attachment_id)
        deduped.append(entry)

    return {"scope": normalized_scope, "attachments": deduped[: max(limit, 1)]}


async def read_chat_attachment(
    attachment_id: str,
    *,
    context: AgentContext,
    max_chars: int = chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS,
) -> dict[str, Any] | None:
    """Read a chat attachment by internal attachment ID.

    Use this when a message-level attachment manifest gives you an `id` for a file.
    It reads the attachment from CopilotGTM-managed storage instead of depending on
    Slack/Teams provider URLs.
    """
    session_id = _session_id_from_context(context)
    allowed_ids = _allowed_attachment_ids_from_context(context)
    if not session_id and not allowed_ids:
        return ToolResult.not_found("No chat attachment context is available for this run.")

    if max_chars <= 0:
        max_chars = chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS

    if attachment_id.startswith(_BOT_CONTEXT_PREFIX):
        return await _read_legacy_bot_context(
            attachment_id=attachment_id, context=context, max_chars=max_chars
        )

    try:
        parsed_attachment_id = UUID(attachment_id)
    except Exception:
        return ToolResult.error("attachment_id must be a valid attachment UUID")

    async with context.acquire_session() as session:
        attachment = await _load_attachment_for_context(
            session=session,
            context=context,
            session_id=session_id,
            allowed_ids=allowed_ids,
            attachment_id=parsed_attachment_id,
        )
        if not attachment:
            return ToolResult.not_found("Attachment not found in this conversation")

    mimetype = (attachment.mimetype or "").lower()

    if not attachment.storage_path:
        return ToolResult.not_found("Attachment storage path unavailable")

    storage = AzureOrganisationBlobStorage(context.organisation_id)
    try:
        extracted: str = ""
        content_type = "text"
        content_bytes = await storage.get_blob_content(attachment.storage_path)

        if is_image_mimetype(mimetype):
            return AgnoToolResult(
                content=(
                    f"Opened image attachment {attachment.filename}. Analyze the image directly before answering."
                ),
                images=[
                    AgnoImage(
                        id=str(attachment.attachment_id),
                        content=content_bytes,
                        format=(attachment.mimetype or "image/png").split("/")[-1],
                        mime_type=attachment.mimetype or "image/png",
                    )
                ],
            )

        if is_audio_mimetype(mimetype):
            extracted = await transcribe_audio(content_bytes, attachment.filename)
            content_type = "audio_transcript"
        else:
            extracted = extract_text_from_bytes(content_bytes, mimetype, attachment.filename)

        if not extracted:
            return {
                "attachment_id": str(attachment.attachment_id),
                "filename": attachment.filename,
                "mimetype": attachment.mimetype,
                "content_type": "unavailable",
                "content": "No readable text could be extracted from this attachment.",
                "truncated": False,
            }

        content, truncated = _truncate_text(extracted, max_chars)
        return {
            "attachment_id": str(attachment.attachment_id),
            "filename": attachment.filename,
            "mimetype": attachment.mimetype,
            "content_type": content_type,
            "content": content,
            "truncated": truncated,
        }
    finally:
        await storage.close()


async def _load_attachment_for_context(
    *,
    session,
    context: AgentContext,
    session_id: UUID | None,
    allowed_ids: set[UUID],
    attachment_id: UUID,
) -> Attachment | None:
    if attachment_id in allowed_ids:
        # This branch is only for current-turn transient access. Session history still uses
        # the persisted message-to-attachment link as the security boundary.
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
    context: AgentContext,
    max_chars: int,
) -> dict[str, Any] | None:
    session_id = _session_id_from_context(context)
    if not session_id:
        return ToolResult.not_found("This conversation has no persisted chat attachment context.")

    try:
        payload = attachment_id.removeprefix(_BOT_CONTEXT_PREFIX)
        message_id_raw, index_raw = payload.split(":", 1)
        message_id = UUID(message_id_raw)
        index = int(index_raw)
    except Exception:
        return ToolResult.error("Invalid bot attachment context ID")

    async with context.acquire_session() as session:
        message = await session.get(ChatMessage, message_id)
        if not message or message.session_id != session_id:
            return ToolResult.not_found("Attachment context not found in this conversation")
        contexts = (message.extra or {}).get("attachment_contexts") or []
        if index < 0 or index >= len(contexts):
            return ToolResult.not_found("Attachment context not found in this conversation")
        ctx_entry = contexts[index]

    content, truncated = _truncate_text(str(ctx_entry.get("content") or ""), max_chars)
    return {
        "attachment_id": attachment_id,
        "filename": ctx_entry.get("filename", "attachment"),
        "mimetype": ctx_entry.get("mimetype", ""),
        "content_type": ctx_entry.get("content_type", "text"),
        "content": content,
        "truncated": truncated,
    }


def create_list_chat_attachments(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the list_chat_attachments tool."""

    async def list_chat_attachments_tool(
        scope: str = "conversation",
        limit: int = 10,
    ) -> dict[str, Any]:
        return await execute_with_capabilities(
            capabilities_getter(),
            tool_name="list_chat_attachments",
            tool_func=list_chat_attachments,
            parameters={"context": context, "scope": scope, "limit": limit},
            description="Listing chat attachments available in this conversation",
            fallback_value={"scope": scope, "attachments": []},
        )

    return list_chat_attachments_tool


def create_read_chat_attachment(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory for the primary read_chat_attachment tool."""

    async def read_chat_attachment_tool(
        attachment_id: str,
        max_chars: int = chat_file_upload_config.ATTACHMENT_READ_MAX_CHARS,
    ) -> dict[str, Any] | None:
        """Read the contents of an attached file using the attachment ID shown in message history."""
        return await execute_with_capabilities(
            capabilities_getter(),
            tool_name="read_chat_attachment",
            tool_func=read_chat_attachment,
            parameters={
                "attachment_id": attachment_id,
                "context": context,
                "max_chars": max_chars,
            },
            description="Reading an attached file from CopilotGTM-managed chat storage",
            fallback_value=ToolResult.not_found("Attachment content unavailable"),
        )

    return read_chat_attachment_tool
