from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy import Result
from sqlmodel import col
from src.domain.models.organisation_tables.chat_session_models import (
    ChatSession,
    ChatType,
)
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger


class ChatSessionService:
    _RETRYABLE_RUNTIME_ERRORS = ("deque mutated during iteration",)

    def __init__(self, db: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.db = db
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._logger = get_logger("chat_session_service")

    NOT_FOUND_MSG = "Chat session not found"

    _MAX_TITLE_LENGTH = 100

    async def create_session(
        self,
        *,
        title: str,
        chat_type: ChatType | str = ChatType.GENERAL_CHAT,
        properties: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> ChatSession:
        if not title or not title.strip():
            raise ValueError("Title cannot be null or empty")

        title = title.strip()
        if len(title) > self._MAX_TITLE_LENGTH:
            title = title[: self._MAX_TITLE_LENGTH - 3].rstrip() + "..."

        # Normalize chat_type in case a raw string is passed
        if isinstance(chat_type, str):
            try:
                chat_type = ChatType(chat_type)
            except Exception:
                chat_type = ChatType.GENERAL_CHAT

        chat = ChatSession.model_validate(
            {
                "user_id": self.user_id,
                "organisation_id": self.organisation_id,
                "title": title,
                "chat_type": chat_type,
                "properties": properties or {},
                "tags": tags or [],
            }
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
            col(ChatSession.session_id) == session_id,
            col(ChatSession.user_id) == self.user_id,
            col(ChatSession.organisation_id) == self.organisation_id,
            col(ChatSession.is_deleted).is_(False),
        )
        res = await self._execute_with_retry(stmt)
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
        title = title.strip()
        if len(title) > self._MAX_TITLE_LENGTH:
            title = title[: self._MAX_TITLE_LENGTH - 3].rstrip() + "..."

        stmt = select(ChatSession).where(
            col(ChatSession.session_id) == session_id,
            col(ChatSession.user_id) == self.user_id,
            col(ChatSession.organisation_id) == self.organisation_id,
            col(ChatSession.is_deleted).is_(False),
        )
        res = await self._execute_with_retry(stmt)
        chat = res.scalars().one_or_none()
        if not chat:
            raise ValueError(self.NOT_FOUND_MSG)

        chat.title = title
        self.db.add(chat)
        await self.db.commit()
        await self.db.refresh(chat)
        return chat

    async def list_sessions(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[ChatSession], str | None]:
        filters = [
            col(ChatSession.user_id) == self.user_id,
            col(ChatSession.organisation_id) == self.organisation_id,
            col(ChatSession.is_deleted).is_(False),
            col(ChatSession.chat_type) != ChatType.TEAMS_CHAT,
        ]

        if cursor:
            ts, sid = decode_cursor(cursor)
            filters.append(
                or_(
                    col(ChatSession.created_at) < ts,
                    and_(
                        col(ChatSession.created_at) == ts,
                        col(ChatSession.session_id) < sid,
                    ),
                )
            )

        stmt = (
            select(ChatSession)
            .where(*filters)
            .order_by(col(ChatSession.created_at).desc(), col(ChatSession.session_id).desc())
            .limit(limit + 1)
        )
        res = await self._execute_with_retry(stmt)
        rows = res.scalars().all()
        has_more = len(rows) == limit + 1
        items = list(rows[:limit])
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].session_id) if has_more else None
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
                col(ChatSession.user_id) == self.user_id,
                col(ChatSession.organisation_id) == self.organisation_id,
                col(ChatSession.is_deleted).is_(False),
                col(ChatSession.chat_type) != ChatType.TEAMS_CHAT,
                col(ChatSession.title).ilike(f"%{query}%"),
            )
            .order_by(col(ChatSession.updated_at).desc(), col(ChatSession.created_at).desc())
            .limit(limit)
        )
        res = await self._execute_with_retry(stmt)
        rows = res.scalars().all()
        return list(rows)

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
            col(ChatSession.session_id) == session_id,
            col(ChatSession.user_id) == self.user_id,
            col(ChatSession.organisation_id) == self.organisation_id,
            col(ChatSession.is_deleted).is_(False),
        )
        res = await self._execute_with_retry(stmt)
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

    async def _execute_with_retry(self, stmt: Any, *, retries: int = 1) -> Result[Any]:
        attempt = 0
        while True:
            try:
                return await self.db.execute(stmt)
            except RuntimeError as exc:
                msg = str(exc).lower()
                if attempt >= retries or not any(
                    token in msg for token in self._RETRYABLE_RUNTIME_ERRORS
                ):
                    raise
                attempt += 1
                self._logger.warning(
                    "Retrying statement after RuntimeError '%s' (attempt %s/%s)",
                    exc,
                    attempt,
                    retries + 1,
                )
                with contextlib.suppress(Exception):
                    await self.db.rollback()
                await asyncio.sleep(0.05)
