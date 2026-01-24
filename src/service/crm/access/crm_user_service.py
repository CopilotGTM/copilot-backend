"""Service for listing app users for CRM owner/team selection."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import User
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class CRMUserServiceError(Exception):
    """Custom exception for CRM user service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CRMUserService:
    """Service for listing organisation users for CRM selection."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("CRMUserService close exception", exc_info=True)

    async def list_users(
        self,
        limit: int = 50,
        cursor: str | None = None,
        q: str | None = None,
    ) -> tuple[list[User], str | None]:
        """List app users for the organisation with cursor pagination."""
        try:
            stmt = select(User).where(col(User.organisation_id) == self.organisation_id)

            if q:
                like_expr = f"%{q.lower()}%"
                stmt = stmt.where(
                    or_(
                        col(User.email).ilike(like_expr),
                        col(User.first_name).ilike(like_expr),
                        col(User.last_name).ilike(like_expr),
                    )
                )

            if cursor:
                ts, user_id = decode_cursor(cursor)
                stmt = stmt.where(
                    (col(User.created_at) < ts)
                    | ((col(User.created_at) == ts) & (col(User.user_id) < user_id))
                )

            stmt = stmt.order_by(col(User.created_at).desc(), col(User.user_id).desc()).limit(
                limit + 1
            )
            result = await self.session.execute(stmt)
            rows = list(result.scalars().all())

            has_more = len(rows) > limit
            items = rows[:limit]

            next_cursor: str | None = None
            if has_more and items:
                last = items[-1]
                next_cursor = encode_cursor(last.created_at, last.user_id)

            return items, next_cursor
        except Exception as exc:
            logger.error("Failed to list CRM users: %s", exc, exc_info=True)
            raise CRMUserServiceError("Failed to list users") from exc


def build_user_display_name(user: User) -> str | None:
    """Build a display name from first and last name."""
    first = user.first_name or ""
    last = user.last_name or ""
    name = f"{first} {last}".strip()
    return name or None
