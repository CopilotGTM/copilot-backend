"""Persist outbound notifications into channel chat history."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.infra.database.sessions import org_async_session_scope


class OutboundChatContextService:
    """Record outbound notifications so later user replies have local context."""

    async def persist_outbound_notification_context(
        self,
        *,
        organisation_id: UUID,
        user_id: UUID,
        chat_type: ChatType,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> UUID | None:
        clean_text = text.strip()
        if not clean_text:
            return None

        async with org_async_session_scope(organisation_id) as session:
            stmt = select(ChatSession).where(
                col(ChatSession.user_id) == user_id,
                col(ChatSession.organisation_id) == organisation_id,
                col(ChatSession.chat_type) == chat_type,
                col(ChatSession.is_deleted).is_(False),
            )
            result = await session.execute(stmt)
            chat_session = result.scalars().first()
            if not chat_session:
                chat_session = ChatSession.model_validate(
                    {
                        "user_id": user_id,
                        "organisation_id": organisation_id,
                        "chat_type": chat_type,
                        "title": self._default_title(chat_type),
                    }
                )
                session.add(chat_session)
                await session.flush()

            msg = ChatMessage(
                session_id=chat_session.session_id,
                role=ChatMessageRole.ASSISTANT,
                message_type=ChatMessageType.GENERAL_MESSAGE,
                content={"text": clean_text},
                extra={
                    "source": "outbound_notification",
                    **(metadata or {}),
                },
            )
            session.add(msg)
            await session.commit()
            return msg.message_id

    @staticmethod
    def _default_title(chat_type: ChatType) -> str:
        if chat_type == ChatType.WHATSAPP_CHAT:
            return "WhatsApp Chat"
        return "Chat"
