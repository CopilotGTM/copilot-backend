from __future__ import annotations

from typing import Optional, Dict, Any, List
from uuid import UUID

from sqlmodel import Session

from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType


class ChatSessionService:
    def __init__(self, db: Session):
        self.db = db

    def create_session(
        self,
        *,
        user_id: UUID,
        organisation_id: UUID,
        title: str,
        chat_type: ChatType | str = ChatType.GENERAL_CHAT,
        properties: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> ChatSession:
        if not title or not title.strip():
            raise ValueError("Title cannot be null or empty")

        if len(title.strip()) > 100:
            raise ValueError("Title cannot exceed 100 characters")

        # Normalize chat_type in case a raw string is passed
        if isinstance(chat_type, str):
            try:
                chat_type = ChatType(chat_type)
            except Exception:
                chat_type = ChatType.GENERAL_CHAT

        chat = ChatSession(
            user_id=user_id,
            organisation_id=organisation_id,
            title=title.strip(),
            chat_type=chat_type,
            properties=properties or {},
            tags=tags or [],
        )

        self.db.add(chat)
        self.db.commit()
        self.db.refresh(chat)
        return chat

