"""Org- and user-scoped persistence operations for the web-chat API.

Routes use this service to create recoverable chat jobs, resolve only attachments
owned by the caller, paginate history, and record feedback without crossing the
tenant or user boundary.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
    ChatMessage,
    ChatMessageAttachmentLink,
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.domain.models.organisation_tables.job_models import (
    ChatMessageJob,
    JobStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.cursor import decode_cursor, encode_cursor


def _to_confidence(val) -> str | None:
    """Convert confidence value to string, handling both string labels and numeric values."""
    if val is None:
        return None

    if isinstance(val, str):
        # Return string confidence as-is if it's a valid value
        label = val.strip().lower()
        valid_labels = {"very_low", "low", "medium", "high", "very_high"}
        if label in valid_labels:
            return label
        return val.strip()  # Return original string if not in mapping

    if isinstance(val, (int, float)):
        # Convert numeric to categorical string (matching agent output)
        if val >= 0.8:
            return "high"
        elif val >= 0.5:
            return "medium"
        else:
            return "low"

    return None


def _col(expr: Any) -> ColumnElement[Any]:
    """Expose SQLModel descriptors as SQLAlchemy expressions for typed queries."""
    return cast(ColumnElement[Any], expr)


def _col_bool(expr: Any) -> ColumnElement[bool]:
    return cast(ColumnElement[bool], expr)


class ChatMessageService:
    """Persist and query web-chat data within one organization and user scope."""

    def __init__(self, db: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.db = db
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def _resolve_attachments(self, attachment_ids: Iterable[UUID] | None) -> list[Attachment]:
        if not attachment_ids:
            return []
        stmt = select(Attachment).where(
            _col(Attachment.attachment_id).in_(list(attachment_ids)),
            _col_bool(Attachment.organisation_id == self.organisation_id),
            _col_bool(Attachment.user_id == self.user_id),
            _col_bool(Attachment.is_deleted).is_(False),
        )
        result = await self.db.execute(stmt)
        rows: list[Attachment] = list(result.scalars().all())
        return rows

    def create_link(self, message_id: UUID, order_att):
        """Create a link between a message and an attachment."""
        order, att = order_att
        link = ChatMessageAttachmentLink(
            message_id=message_id,
            attachment_id=att.attachment_id,
            attachment_order=order,
        )
        self.db.add(link)

    async def create_user_message(
        self,
        *,
        session_id: UUID,
        content: str,
        user_timezone: str | None = None,
        attachment_ids: Iterable[UUID] | None = None,
        message_type: ChatMessageType = ChatMessageType.GENERAL_MESSAGE,
        role: ChatMessageRole = ChatMessageRole.USER,
        job_type: str = "general_message",
    ) -> tuple[ChatMessage, ChatMessageJob, str]:
        """Create a user message and a corresponding job.

        Returns:
            tuple: (user_message, job, assistant_message_id_str)
        """

        # Guard: Ensure session belongs to caller's org/user
        sess_stmt = select(ChatSession).where(
            _col_bool(ChatSession.session_id == session_id),
            _col_bool(ChatSession.organisation_id == self.organisation_id),
            _col_bool(ChatSession.user_id == self.user_id),
            _col_bool(ChatSession.is_deleted).is_(False),
        )
        result = await self.db.execute(sess_stmt)
        session = result.scalar_one_or_none()
        if not session:
            raise ValueError("Chat session not found")

        # Shape content as JSON (allows future rich content)
        content_payload = {"text": content}

        message = ChatMessage(
            session_id=session_id,
            role=role,
            message_type=message_type,
            content=content_payload,
        )

        # Resolve and link attachments
        attachments = await self._resolve_attachments(attachment_ids)

        self.db.add(message)
        await self.db.flush()  # ensure message_id populated for link table
        session.updated_at = message.created_at

        # Link attachments in parallel for improved performance
        if attachments:
            with ThreadPoolExecutor() as executor:
                # Bind the flushed message ID once so every link created by the
                # executor belongs to the same parent message.
                create_link_with_message_id = partial(self.create_link, message.message_id)
                executor.map(create_link_with_message_id, enumerate(attachments))

        # Store every identifier needed to reconstruct agent work before the API
        # attempts broker publication; the recovery sweep reads this metadata.
        assistant_message_id = str(uuid4())
        job_metadata: dict[str, Any] = {
            "session_id": str(session_id),
            "user_message_id": str(message.message_id),
            "assistant_message_id": assistant_message_id,
            "current_datetime": message.created_at.astimezone(UTC).isoformat(),
            "enqueue_status": "pending",
            "enqueue_attempts": 0,
            "celery_task_id": None,
            "last_enqueue_error": None,
        }
        if user_timezone:
            # Preserve the user's local timezone so relative dates in the agent match
            # the caller's day rather than the worker region.
            job_metadata["user_timezone"] = user_timezone
        job = ChatMessageJob(
            message_id=message.message_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            user_id=self.user_id,
            job_metadata=job_metadata,
        )
        self.db.add(job)

        # The commit is handled by the caller to ensure atomicity with task queueing.
        await self.db.refresh(message)
        await self.db.refresh(job)
        return message, job, assistant_message_id

    async def list_messages(
        self,
        *,
        session_id: UUID,
        limit: int = 20,
        cursor: str | None = None,
        role: ChatMessageRole | None = None,
        message_type: ChatMessageType | None = None,
    ) -> tuple[list[ChatMessage], str | None]:
        """List messages for a session with stable cursor-based pagination.

        Orders by created_at desc, message_id desc to ensure a consistent keyset.
        Cursor encodes (created_at, message_id).
        """
        # Ensure session belongs to caller
        sess_stmt = select(ChatSession).where(
            _col_bool(ChatSession.session_id == session_id),
            _col_bool(ChatSession.organisation_id == self.organisation_id),
            _col_bool(ChatSession.user_id == self.user_id),
            _col_bool(ChatSession.is_deleted).is_(False),
        )
        session_res = await self.db.execute(sess_stmt)
        session = session_res.scalar_one_or_none()
        if not session:
            raise ValueError("Chat session not found")

        filters: list[ColumnElement[bool]] = [
            _col_bool(ChatMessage.session_id == session_id),
        ]
        if role:
            filters.append(_col_bool(ChatMessage.role == role))
        if message_type:
            filters.append(_col_bool(ChatMessage.message_type == message_type))

        if cursor:
            ts, mid = decode_cursor(cursor)
            filters.append(
                or_(
                    _col_bool(ChatMessage.created_at < ts),
                    and_(
                        _col_bool(ChatMessage.created_at == ts),
                        _col_bool(ChatMessage.message_id < mid),
                    ),
                )
            )

        stmt = (
            select(ChatMessage)
            .where(*filters)
            .order_by(_col(ChatMessage.created_at).desc(), _col(ChatMessage.message_id).desc())
            .limit(limit + 1)
        )
        message_res = await self.db.execute(stmt)
        rows: list[ChatMessage] = list(message_res.scalars().all())
        has_more = len(rows) == limit + 1
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].message_id) if has_more else None
        )
        return items, next_cursor

    async def get_message_attachments(
        self, messages: list[ChatMessage]
    ) -> dict[UUID, list[Attachment]]:
        """Return attachments for messages keyed by message_id in attachment_order."""
        if not messages:
            return {}
        msg_ids = [m.message_id for m in messages]
        message_id_col = _col(ChatMessageAttachmentLink.message_id)
        attachment_id_col = _col(ChatMessageAttachmentLink.attachment_id)
        res = await self.db.execute(
            select(
                message_id_col,
                Attachment,
                ChatMessageAttachmentLink.attachment_order,
            )
            .join(
                Attachment,
                _col_bool(attachment_id_col == Attachment.attachment_id),
            )
            .where(
                _col_bool(message_id_col.in_(msg_ids)),
                _col_bool(Attachment.organisation_id == self.organisation_id),
                _col_bool(Attachment.user_id == self.user_id),
                _col_bool(Attachment.is_deleted).is_(False),
            )
            .order_by(
                message_id_col,
                _col(ChatMessageAttachmentLink.attachment_order),
            )
        )
        result_map: dict[UUID, list[Attachment]] = {mid: [] for mid in msg_ids}
        for mid, att, _ord in res.all():
            result_map[mid].append(att)
        return result_map

    async def build_attachment_urls(
        self,
        *,
        attachments_by_msg: dict[UUID, list[Attachment]],
        minutes: int = 30,
    ) -> dict[UUID, list[dict[str, Any]]]:
        """Build display/download SAS URLs for attachments per message.

        Returns dict[message_id] -> list of dicts conforming to ChatAttachmentView.
        """
        storage = AzureOrganisationBlobStorage(self.organisation_id)

        async def _sas(
            path: str | None,
            *,
            content_type: str | None = None,
        ) -> str | None:
            if not path:
                return None
            try:
                return await storage.generate_read_sas(
                    path,
                    minutes=minutes,
                    content_type=content_type,
                )
            except Exception:
                return None

        out: dict[UUID, list[dict[str, Any]]] = {}
        for mid, atts in attachments_by_msg.items():
            enriched: list[dict[str, Any]] = []
            for att in atts:
                enriched.append(
                    {
                        "attachment_id": att.attachment_id,
                        "filename": att.filename,
                        "mimetype": att.mimetype,
                        "thumbnail_small_url": await _sas(
                            getattr(att, "thumbnail_small_path", None)
                        ),
                        "playback_url": await _sas(
                            getattr(att, "storage_path", None),
                            content_type=att.mimetype,
                        )
                        if _is_completed_audio(att)
                        else None,
                    }
                )
            out[mid] = enriched
        with contextlib.suppress(Exception):
            await storage.close()
        return out

    def build_message_list_items(
        self,
        messages: list[ChatMessage],
        attachments_urls: dict[UUID, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Build list items with enriched metadata for message listing."""
        items = []
        for m in messages:
            agent = getattr(m, "agent_data", None) or {}
            extra = getattr(m, "extra", None) or {}
            text = m.content.get("text") if isinstance(m.content, dict) else m.content

            items.append(
                {
                    "message_id": m.message_id,
                    "session_id": m.session_id,
                    "role": m.role,
                    "message_type": m.message_type,
                    "content": text,
                    "is_edited": m.is_edited,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                    "attachments": attachments_urls.get(m.message_id, []),
                    "confidence_score": _to_confidence(
                        extra.get("confidence_score") if isinstance(extra, dict) else None
                    ),
                    "sources": (agent.get("sources") if isinstance(agent, dict) else None),
                    "suggestions": (
                        agent.get("suggested_questions") if isinstance(agent, dict) else None
                    ),
                    "feedback_score": getattr(m, "feedback_score", None),
                }
            )
        return items

    async def update_message_feedback(
        self,
        *,
        message_id: UUID,
        feedback_score: int,
    ) -> ChatMessage:
        """Update the feedback score for a message.

        Args:
            message_id: The ID of the message to update
            feedback_score: The feedback score (1 for thumbs up, -1 for thumbs down, 0 to clear)

        Returns:
            The updated ChatMessage

        Raises:
            ValueError: If the message is not found or doesn't belong to the user
        """
        # Validate feedback score
        if feedback_score not in {-1, 0, 1}:
            raise ValueError("feedback_score must be -1, 0, or 1")

        # Find the message and verify ownership through session
        stmt = (
            select(ChatMessage)
            .join(
                ChatSession,
                _col_bool(ChatMessage.session_id == ChatSession.session_id),
            )
            .where(
                _col_bool(ChatMessage.message_id == message_id),
                _col_bool(ChatSession.organisation_id == self.organisation_id),
                _col_bool(ChatSession.user_id == self.user_id),
                _col_bool(ChatSession.is_deleted).is_(False),
            )
        )
        result = await self.db.execute(stmt)
        message = result.scalar_one_or_none()

        if not message:
            raise ValueError(
                "Message not found or you don't have permission to update this message"
            )

        # Zero is the API's clear-feedback command, while the database represents
        # an unrated message as NULL.
        message.feedback_score = feedback_score if feedback_score != 0 else None

        # Commit is handled by the caller for transaction control
        await self.db.flush()
        await self.db.refresh(message)
        return message

    async def close(self) -> None:
        """Close any resources held by the service, including the database session."""
        if self.db:
            await self.db.close()


def _is_completed_audio(attachment: Attachment) -> bool:
    status = getattr(attachment, "status", None)
    status_value = status.value if hasattr(status, "value") else str(status or "")
    return (
        status_value == AttachmentStatus.COMPLETED.value
        and str(getattr(attachment, "mimetype", "") or "").lower().startswith("audio/")
        and bool(getattr(attachment, "storage_path", None))
    )
