"""CRM service layer for accessing activities (tasks, calls, meetings, etc.)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMActivity
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger("tasks_access_service")


class TasksAccessServiceError(Exception):
    """Custom exception for tasks access service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TasksAccessService:
    """Service for retrieving CRM deal tasks with filtering and pagination."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("TasksAccessService close exception", exc_info=True)

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def list_tasks_by_deal(
        self,
        deal_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        statuses: Iterable[str] | None = None,
        priorities: Iterable[str] | None = None,
        task_types: Iterable[str] | None = None,
    ) -> tuple[list[CRMActivity], str | None]:
        """Return paginated activities for a specific deal."""

        c = cast(Any, CRMActivity).__table__.c
        stmt = select(CRMActivity).where(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )

        if statuses:
            stmt = stmt.where(c.status.in_(list(statuses)))

        if priorities:
            stmt = stmt.where(c.priority.in_(list(priorities)))

        if task_types:
            stmt = stmt.where(c.activity_type.in_(list(task_types)))

        if cursor:
            try:
                ts, activity_id = decode_cursor(cursor)
            except Exception as exc:
                raise TasksAccessServiceError("Invalid cursor", status_code=400) from exc

            stmt = stmt.where(
                or_(
                    c.created_at < ts,
                    and_(c.created_at == ts, c.activity_id < activity_id),
                )
            )

        stmt = stmt.order_by(c.created_at.desc(), c.activity_id.desc()).limit(limit + 1)

        res = await self.org_session.execute(stmt)
        rows: list[CRMActivity] = list(res.scalars().all())

        has_more = len(rows) == limit + 1
        items = rows[:limit]

        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.created_at, last.activity_id)

        return items, next_cursor

    async def get_task_by_id(self, task_id: UUID) -> CRMActivity | None:
        """Fetch a single activity by ID (backwards compatible with task_id param name)."""

        try:
            c = cast(Any, CRMActivity).__table__.c
            stmt = select(CRMActivity).where(
                c.activity_id == task_id,
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
            )
            res = await self.org_session.execute(stmt)
            return res.scalar_one_or_none()
        except Exception as exc:
            logger.error("Failed to get task %s: %s", task_id, exc, exc_info=True)
            raise TasksAccessServiceError("Failed to retrieve task") from exc
