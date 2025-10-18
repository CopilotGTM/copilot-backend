from __future__ import annotations

import contextlib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
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


class ChatMessageService:
    def __init__(self, db: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.db = db
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def _resolve_attachments(self, attachment_ids: Iterable[UUID] | None) -> list[Attachment]:
        if not attachment_ids:
            return []
        stmt = select(Attachment).where(
            Attachment.attachment_id.in_(list(attachment_ids)),
            Attachment.organisation_id == self.organisation_id,
            Attachment.user_id == self.user_id,
            Attachment.is_deleted.is_(False),
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

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
            ChatSession.session_id == session_id,
            ChatSession.organisation_id == self.organisation_id,
            ChatSession.user_id == self.user_id,
            ChatSession.is_deleted.is_(False),
        )
        result = await self.db.execute(sess_stmt)
        session = result.scalar_one_or_none()
        if not session:
            raise ValueError("Chat session not found")

        # Shape content as JSON (allows future rich content)
        content = {"text": content}

        message = ChatMessage(
            session_id=session_id,
            role=role,
            message_type=message_type,
            content=content,
        )

        # Resolve and link attachments
        attachments = await self._resolve_attachments(attachment_ids)

        self.db.add(message)
        await self.db.flush()  # ensure message_id populated for link table

        # Link attachments in parallel for improved performance
        if attachments:
            with ThreadPoolExecutor() as executor:
                # Create a partial function that includes message_id
                create_link_with_message_id = partial(self.create_link, message.message_id)
                executor.map(create_link_with_message_id, enumerate(attachments))

        # Create a pending job for processing this message (assistant reply generation)
        assistant_message_id = str(uuid4())
        job = ChatMessageJob(
            message_id=message.message_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            job_metadata={
                "session_id": str(session_id),
                "user_message_id": str(message.message_id),
                "assistant_message_id": assistant_message_id,
            },
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
            ChatSession.session_id == session_id,
            ChatSession.organisation_id == self.organisation_id,
            ChatSession.user_id == self.user_id,
            ChatSession.is_deleted.is_(False),
        )
        res = await self.db.execute(sess_stmt)
        session = res.scalar_one_or_none()
        if not session:
            raise ValueError("Chat session not found")

        filters = [
            ChatMessage.session_id == session_id,
        ]
        if role:
            filters.append(ChatMessage.role == role)
        if message_type:
            filters.append(ChatMessage.message_type == message_type)

        if cursor:
            ts, mid = decode_cursor(cursor)
            filters.append(
                or_(
                    ChatMessage.created_at < ts,
                    and_(ChatMessage.created_at == ts, ChatMessage.message_id < mid),
                )
            )

        stmt = (
            select(ChatMessage)
            .where(*filters)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.message_id.desc())
            .limit(limit + 1)
        )
        res = await self.db.execute(stmt)
        rows = res.scalars().all()
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
        res = await self.db.execute(
            select(
                ChatMessageAttachmentLink.message_id,
                Attachment,
                ChatMessageAttachmentLink.attachment_order,
            )
            .join(
                Attachment,
                ChatMessageAttachmentLink.attachment_id == Attachment.attachment_id,
            )
            .where(
                ChatMessageAttachmentLink.message_id.in_(msg_ids),
                Attachment.organisation_id == self.organisation_id,
                Attachment.user_id == self.user_id,
                Attachment.is_deleted.is_(False),
            )
            .order_by(
                ChatMessageAttachmentLink.message_id,
                ChatMessageAttachmentLink.attachment_order,
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
        minutes: int = 120,
    ) -> dict[UUID, list[dict[str, Any]]]:
        """Build thumbnail SAS URLs for attachments per message.

        Returns dict[message_id] -> list of dicts conforming to ChatAttachmentView.
        """
        storage = AzureOrganisationBlobStorage(self.organisation_id)

        async def _sas(path: str | None) -> str | None:
            if not path:
                return None
            try:
                return await storage.generate_read_sas(path, minutes=minutes)
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
                }
            )
        return items
