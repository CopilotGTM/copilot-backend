from __future__ import annotations

from typing import Optional, Dict, Any, List
from uuid import UUID

from sqlmodel import Session, select
from sqlalchemy import or_, and_
from src.infra.utils.cursor import encode_cursor, decode_cursor

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

    def delete_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        organisation_id: UUID,
    ) -> ChatSession:
        """Soft delete a chat session scoped to the caller's org and user.

        Raises ValueError if not found or already deleted.
        """
        stmt = select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.user_id == user_id,
            ChatSession.organisation_id == organisation_id,
            ChatSession.is_deleted.is_(False),
        )
        chat = self.db.exec(stmt).one_or_none()
        if not chat:
            raise ValueError("Chat session not found")

        chat.is_deleted = True
        self.db.add(chat)
        self.db.commit()
        self.db.refresh(chat)
        return chat

    def rename_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        organisation_id: UUID,
        title: str,
    ) -> ChatSession:
        """Rename a chat session, scoped by user/org and not deleted."""
        if not title or not title.strip():
            raise ValueError("Title cannot be null or empty")
        if len(title.strip()) > 100:
            raise ValueError("Title cannot exceed 100 characters")

        stmt = select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.user_id == user_id,
            ChatSession.organisation_id == organisation_id,
            ChatSession.is_deleted.is_(False),
        )
        chat = self.db.exec(stmt).one_or_none()
        if not chat:
            raise ValueError("Chat session not found")

        chat.title = title.strip()
        self.db.add(chat)
        self.db.commit()
        self.db.refresh(chat)
        return chat

    def list_sessions(
        self,
        *,
        user_id: UUID,
        organisation_id: UUID,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> tuple[list[ChatSession], Optional[str]]:
        filters = [
            ChatSession.user_id == user_id,
            ChatSession.organisation_id == organisation_id,
            ChatSession.is_deleted.is_(False),
        ]

        if cursor:
            ts, sid = decode_cursor(cursor)
            filters.append(
                or_(
                    ChatSession.created_at < ts,
                    and_(
                        ChatSession.created_at == ts,
                        ChatSession.session_id < sid,
                    ),
                )
            )

        stmt = (
            select(ChatSession)
            .where(*filters)
            .order_by(ChatSession.created_at.desc(), ChatSession.session_id.desc())
            .limit(limit + 1)
        )
        rows = self.db.exec(stmt).all()
        has_more = len(rows) == limit + 1
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].session_id) if has_more else None
        )
        return items, next_cursor
