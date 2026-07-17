"""Tools for accessing chat attachments in the current conversation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from agno.media import Image as AgnoImage
from agno.tools.function import ToolResult as AgnoToolResult
from sqlmodel import col, select

from src.agents.tools.base import ToolResult, execute_with_capabilities
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
    ChatMessage,
    ChatMessageAttachmentLink,
)
from src.infra.config.configs import chat_file_upload_config
from src.service.chat.chat_attachment_reader import read_attachment_content

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
                .order_by(col(ChatMessage.created_at).desc())
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
                        col(ChatMessageAttachmentLink.attachment_id)
                        == col(Attachment.attachment_id),
                    )
                    .where(
                        col(ChatMessageAttachmentLink.message_id).in_(list(message_map.keys())),
                        Attachment.organisation_id == context.organisation_id,
                        col(Attachment.is_deleted).is_(False),
                        col(Attachment.status).notin_(
                            [
                                AttachmentStatus.PENDING_UPLOAD.value,
                                AttachmentStatus.FAILED.value,
                            ]
                        ),
                    )
                    .order_by(col(ChatMessageAttachmentLink.attachment_order))
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
                col(Attachment.attachment_id).in_(list(allowed_ids)),
                Attachment.organisation_id == context.organisation_id,
                col(Attachment.is_deleted).is_(False),
                col(Attachment.status).notin_(
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
) -> dict[str, Any] | AgnoToolResult:
    """Read a conversation attachment and adapt neutral content for Agno."""
    result = await read_attachment_content(
        attachment_id,
        context=context,
        max_chars=max_chars,
    )
    if result.error_code:
        message = result.error or "Attachment content unavailable"
        if result.error_code == "invalid":
            return ToolResult.error(message)
        return ToolResult.not_found(message)

    if result.kind == "image" and result.content_bytes is not None:
        mimetype = result.mimetype or "image/png"
        return AgnoToolResult(
            content=(
                f"Opened image attachment {result.filename}. "
                "Analyze the image directly before answering."
            ),
            images=[
                AgnoImage(
                    id=result.attachment_id,
                    content=result.content_bytes,
                    format=mimetype.split("/")[-1],
                    mime_type=mimetype,
                )
            ],
        )

    return {
        "attachment_id": result.attachment_id,
        "filename": result.filename,
        "mimetype": result.mimetype,
        "content_type": result.kind,
        "content": result.text or "",
        "truncated": result.truncated,
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
