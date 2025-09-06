from __future__ import annotations

from typing import Iterable, List, Optional, Dict, Any
from uuid import UUID

from sqlmodel import Session, select

from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
    ChatMessageRole,
    ChatMessageType,
    Attachment,
    ChatMessageAttachmentLink,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.domain.models.organisation_tables.job_models import (
    ChatMessageJob,
    JobStatus,
)


class ChatMessageService:
    def __init__(self, db: Session, organisation_id: UUID, user_id: UUID):
        self.db = db
        self.organisation_id = organisation_id
        self.user_id = user_id

    def _resolve_attachments(
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
        return self.db.exec(stmt).all()

    def create_user_message(
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

        # Guard: ensure session belongs to caller's org/user
        sess_stmt = select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.organisation_id == self.organisation_id,
            ChatSession.user_id == self.user_id,
            ChatSession.is_deleted.is_(False),
        )
        session = self.db.exec(sess_stmt).one_or_none()
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
        attachments = self._resolve_attachments(attachment_ids)

        self.db.add(message)
        self.db.flush()  # ensure message_id populated for link table

        for order, att in enumerate(attachments):
            link = ChatMessageAttachmentLink(
                message_id=message.message_id,
                attachment_id=att.attachment_id,
                attachment_order=order,
            )
            self.db.add(link)

        # Create a pending job for processing this message (assistant reply generation)
        job = ChatMessageJob(
            message_id=message.message_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            job_metadata={},
        )
        self.db.add(job)

        # The commit is handled by the caller to ensure atomicity with task queueing.
        self.db.refresh(message)
        self.db.refresh(job)
        return message, job
