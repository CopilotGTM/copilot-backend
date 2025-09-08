from __future__ import annotations

from typing import Iterable, List, Optional, Dict, Any
from uuid import UUID
from functools import partial

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
    ChatMessageRole,
    ChatMessageType,
    Attachment,
    ChatMessageAttachmentLink,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from concurrent.futures import ThreadPoolExecutor
from src.domain.models.organisation_tables.job_models import (
    ChatMessageJob,
    JobStatus,
)


class ChatMessageService:
    def __init__(self, db: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.db = db
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def _resolve_attachments(
        self, attachment_ids: Optional[Iterable[UUID]]
    ) -> List[Attachment]:
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
        attachment_ids: Optional[Iterable[UUID]] = None,
        message_type: ChatMessageType = ChatMessageType.GENERAL_MESSAGE,
        role: ChatMessageRole = ChatMessageRole.USER,
        job_type: str = "general_message",
    ) -> tuple[ChatMessage, ChatMessageJob]:
        """Create a user message and a corresponding job."""

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
        job = ChatMessageJob(
            message_id=message.message_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            job_metadata={
                "session_id": str(session_id),
                "message_id": str(message.message_id),
            },
        )
        self.db.add(job)

        # The commit is handled by the caller to ensure atomicity with task queueing.
        await self.db.refresh(message)
        await self.db.refresh(job)
        return message, job
