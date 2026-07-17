from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Literal
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy import Result
from sqlmodel import col
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.chat_session_access_models import (
    ChatSessionAccess,
)
from src.domain.models.organisation_tables.chat_session_models import (
    ChatSession,
    ChatType,
)
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.service.chat.chat_access import resolve_owned_chat_session


class ChatSessionService:
    _RETRYABLE_RUNTIME_ERRORS = ("deque mutated during iteration",)

    def __init__(
        self,
        db: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
        *,
        master_db: AsyncSession | None = None,
    ):
        self.db = db
        self.master_db = master_db
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._logger = get_logger("chat_session_service")

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
        chat = await resolve_owned_chat_session(
            self.db,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            session_id=session_id,
        )

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

        chat = await resolve_owned_chat_session(
            self.db,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            session_id=session_id,
        )

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
        chat_type: str | None = None,
        scope: Literal["all", "owned", "shared"] = "all",
    ) -> tuple[list[ChatSession], str | None]:
        filters = [
            col(ChatSession.organisation_id) == self.organisation_id,
            col(ChatSession.is_deleted).is_(False),
            col(ChatSession.chat_type) != ChatType.TEAMS_CHAT,
        ]

        # Access is resolved before ordering so owned and shared chats use one
        # canonical updated_at/session_id cursor instead of two merged pages.
        grant_exists = exists(
            select(col(ChatSessionAccess.session_id)).where(
                col(ChatSessionAccess.organisation_id) == self.organisation_id,
                col(ChatSessionAccess.session_id) == ChatSession.session_id,
                col(ChatSessionAccess.viewer_user_id) == self.user_id,
            )
        )
        if scope == "owned":
            filters.append(col(ChatSession.user_id) == self.user_id)
        elif scope == "shared":
            filters.append(grant_exists)
        else:
            filters.append(or_(col(ChatSession.user_id) == self.user_id, grant_exists))

        if chat_type:
            filters.append(col(ChatSession.chat_type) == chat_type)

        if cursor:
            ts, sid = decode_cursor(cursor)
            filters.append(
                or_(
                    col(ChatSession.updated_at) < ts,
                    and_(
                        col(ChatSession.updated_at) == ts,
                        col(ChatSession.session_id) < sid,
                    ),
                )
            )

        stmt = (
            select(ChatSession)
            .where(*filters)
            .order_by(col(ChatSession.updated_at).desc(), col(ChatSession.session_id).desc())
            .limit(limit + 1)
        )
        res = await self._execute_with_retry(stmt)
        rows = res.scalars().all()
        has_more = len(rows) == limit + 1
        items = list(rows[:limit])
        next_cursor = (
            encode_cursor(items[-1].updated_at, items[-1].session_id) if has_more else None
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
        chat = await resolve_owned_chat_session(
            self.db,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            session_id=session_id,
        )

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

    async def grant_viewer(
        self,
        *,
        session_id: UUID,
        viewer_user_id: UUID,
    ) -> ChatSessionAccess:
        """Grant an active same-organisation user read-only access to an owned chat.

        The composite database key makes repeated and concurrent grants converge
        on one row; callers receive that effective row in either case.
        """
        session = await resolve_owned_chat_session(
            self.db,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            session_id=session_id,
        )
        if viewer_user_id == session.user_id:
            raise ValueError("Chat owner already has access")

        # The organisation filter is the tenant boundary. A supplied user UUID
        # alone must never make a user in another organisation shareable.
        if self.master_db is None:
            raise RuntimeError("Master database session required for chat sharing")
        viewer_result = await self.master_db.execute(
            select(User).where(
                col(User.user_id) == viewer_user_id,
                col(User.organisation_id) == self.organisation_id,
                col(User.admin_status) == AdminUserStatus.APPROVED,
            )
        )
        if viewer_result.scalar_one_or_none() is None:
            raise ValueError("Viewer user not found")

        statement = (
            pg_insert(ChatSessionAccess)
            .values(
                organisation_id=self.organisation_id,
                session_id=session_id,
                viewer_user_id=viewer_user_id,
                granted_by_user_id=self.user_id,
            )
            .on_conflict_do_nothing(index_elements=["session_id", "viewer_user_id"])
        )
        await self.db.execute(statement)
        await self.db.commit()

        # Read back the canonical row because the insert may have been a no-op
        # when another request created the same grant concurrently.
        result = await self.db.execute(
            select(ChatSessionAccess).where(
                col(ChatSessionAccess.organisation_id) == self.organisation_id,
                col(ChatSessionAccess.session_id) == session_id,
                col(ChatSessionAccess.viewer_user_id) == viewer_user_id,
            )
        )
        return result.scalar_one()

    async def revoke_viewer(
        self,
        *,
        session_id: UUID,
        viewer_user_id: UUID,
    ) -> None:
        """Idempotently revoke a viewer grant from an owned chat session."""
        await resolve_owned_chat_session(
            self.db,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            session_id=session_id,
        )
        await self.db.execute(
            delete(ChatSessionAccess).where(
                col(ChatSessionAccess.organisation_id) == self.organisation_id,
                col(ChatSessionAccess.session_id) == session_id,
                col(ChatSessionAccess.viewer_user_id) == viewer_user_id,
            )
        )
        # Commit even when no row matched so retries have the same successful contract.
        await self.db.commit()

    async def list_viewers(
        self,
        *,
        session_id: UUID,
    ) -> list[ChatSessionAccess]:
        """List explicit viewer grants for a chat owned by the caller."""
        await resolve_owned_chat_session(
            self.db,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            session_id=session_id,
        )
        # Both identifiers remain in SQL so a session UUID cannot bypass tenant scope.
        result = await self.db.execute(
            select(ChatSessionAccess)
            .where(
                col(ChatSessionAccess.organisation_id) == self.organisation_id,
                col(ChatSessionAccess.session_id) == session_id,
            )
            .order_by(
                col(ChatSessionAccess.created_at),
                col(ChatSessionAccess.viewer_user_id),
            )
        )
        return list(result.scalars().all())

    async def list_shared_with_me(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[tuple[ChatSession, ChatSessionAccess, User | None]], str | None]:
        """List chats shared with the caller, including the grantor identity."""
        filters = [
            col(ChatSessionAccess.organisation_id) == self.organisation_id,
            col(ChatSessionAccess.viewer_user_id) == self.user_id,
            col(ChatSession.organisation_id) == self.organisation_id,
            col(ChatSession.is_deleted).is_(False),
            col(ChatSession.chat_type) != ChatType.TEAMS_CHAT,
        ]
        if cursor:
            ts, sid = decode_cursor(cursor)
            filters.append(
                or_(
                    col(ChatSessionAccess.created_at) < ts,
                    and_(
                        col(ChatSessionAccess.created_at) == ts,
                        col(ChatSessionAccess.session_id) < sid,
                    ),
                )
            )

        stmt = (
            select(ChatSession, ChatSessionAccess)
            .select_from(ChatSessionAccess)
            .join(
                ChatSession,
                and_(
                    col(ChatSession.session_id) == ChatSessionAccess.session_id,
                    col(ChatSession.organisation_id) == ChatSessionAccess.organisation_id,
                ),
            )
            .where(*filters)
            .order_by(
                col(ChatSessionAccess.created_at).desc(),
                col(ChatSessionAccess.session_id).desc(),
            )
            .limit(limit + 1)
        )
        result = await self._execute_with_retry(stmt)
        rows = [
            type_cast(tuple[ChatSession, ChatSessionAccess], tuple(row)) for row in result.all()
        ]
        has_more = len(rows) == limit + 1
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1][1].created_at, items[-1][1].session_id) if has_more else None
        )
        if not items:
            return [], next_cursor

        if self.master_db is None:
            raise RuntimeError("Master database session required for chat sharing")
        grantor_ids = {grant.granted_by_user_id for _, grant in items}
        # User identities live in public.users; the organisation filter prevents
        # an access row from resolving metadata for a user in another tenant.
        user_result = await self.master_db.execute(
            select(User).where(
                col(User.organisation_id) == self.organisation_id,
                col(User.user_id).in_(grantor_ids),
            )
        )
        users_by_id = {user.user_id: user for user in user_result.scalars().all()}
        enriched_items = [
            (chat, grant, users_by_id.get(grant.granted_by_user_id)) for chat, grant in items
        ]
        return enriched_items, next_cursor

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
