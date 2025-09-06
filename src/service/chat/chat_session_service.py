from __future__ import annotations

from typing import Optional, Dict, Any, List
from uuid import UUID

from sqlalchemy import or_, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.infra.utils.cursor import encode_cursor, decode_cursor

from src.domain.models.organisation_tables.chat_session_models import (
    ChatSession,
    ChatType,
)


class ChatSessionService:
    def __init__(self, db: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.db = db
        self.organisation_id = organisation_id
        self.user_id = user_id

    NOT_FOUND_MSG = "Chat session not found"

    async def create_session(
        self,
        *,
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
            user_id=self.user_id,
            organisation_id=self.organisation_id,
            title=title.strip(),
            chat_type=chat_type,
            properties=properties or {},
            tags=tags or [],
        )
        self.db.add(chat)
        await self.db.commit()
        await self.db.refresh(chat)
        return chat

    async def delete_session(
        self,
        *,
        session_id: UUID,
    ) -> ChatSession:
        """Soft delete a chat session scoped to the caller's org and user.

        Raises ValueError if not found or already deleted.
        """
        stmt = select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.user_id == self.user_id,
            ChatSession.organisation_id == self.organisation_id,
            ChatSession.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        chat = res.scalars().one_or_none()
        if not chat:
            raise ValueError(self.NOT_FOUND_MSG)

        chat.is_deleted = True
        self.db.add(chat)
        await self.db.commit()
        await self.db.refresh(chat)
        return chat

    async def rename_session(
        self,
        *,
        session_id: UUID,
        title: str,
    ) -> ChatSession:
        """Rename a chat session, scoped by user/org and not deleted."""
        if not title or not title.strip():
            raise ValueError("Title cannot be null or empty")
        if len(title.strip()) > 100:
            raise ValueError("Title cannot exceed 100 characters")

        stmt = select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.user_id == self.user_id,
            ChatSession.organisation_id == self.organisation_id,
            ChatSession.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        chat = res.scalars().one_or_none()
        if not chat:
            raise ValueError(self.NOT_FOUND_MSG)

        chat.title = title.strip()
        self.db.add(chat)
        await self.db.commit()
        await self.db.refresh(chat)
        return chat

    async def list_sessions(
        self,
        *,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> tuple[list[ChatSession], Optional[str]]:
        filters = [
            ChatSession.user_id == self.user_id,
            ChatSession.organisation_id == self.organisation_id,
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
        res = await self.db.execute(stmt)
        rows = res.scalars().all()
        has_more = len(rows) == limit + 1
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].session_id)
            if has_more
            else None
        )
        return items, next_cursor

    async def search_sessions(
        self,
        *,
        q: str,
        limit: int = 10,
    ) -> list[ChatSession]:
        """Quick title search using ILIKE on title, scoped by user/org.

        This is a best‑effort, non‑paginated search suitable for typeahead.
        For large datasets consider adding a GIN/Trigram index or full‑text search.
        """
        query = q.strip()
        if not query:
            raise ValueError("Empty search query")

        if len(query) > 50:
            raise ValueError("Search query cannot exceed 50 characters")

        stmt = (
            select(ChatSession)
            .where(
                ChatSession.user_id == self.user_id,
                ChatSession.organisation_id == self.organisation_id,
                ChatSession.is_deleted.is_(False),
                ChatSession.title.ilike(f"%{query}%"),
            )
            .order_by(ChatSession.updated_at.desc(), ChatSession.created_at.desc())
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        rows = res.scalars().all()
        return rows

    async def set_favorite(
        self,
        *,
        session_id: UUID,
        favorite: bool,
    ) -> ChatSession:
        """Mark/unmark a chat as favorite using the JSONB properties field.

        Avoids schema migration; can be replaced with a dedicated column later.
        """
        stmt = select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.user_id == self.user_id,
            ChatSession.organisation_id == self.organisation_id,
            ChatSession.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        chat = res.scalars().one_or_none()
        if not chat:
            raise ValueError(self.NOT_FOUND_MSG)

        props = dict(chat.properties or {})
        if favorite:
            props["favorite"] = True
        else:
            # remove key to keep JSON small; reader treats missing as False
            props.pop("favorite", None)
        chat.properties = props
        self.db.add(chat)
        await self.db.commit()
        await self.db.refresh(chat)
        return chat
