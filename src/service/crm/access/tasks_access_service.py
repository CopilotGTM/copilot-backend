"""CRM service layer for accessing activities (tasks, calls, meetings, etc.)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMActivity
from src.infra.utils.logger import get_logger

logger = get_logger("tasks_access_service")

_TASK_ACTIVITY_TYPES: set[str] = {"TASK", "TODO"}


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
        page: int = 1,
        statuses: Iterable[str] | None = None,
        priorities: Iterable[str] | None = None,
        task_types: Iterable[str] | None = None,
    ) -> tuple[list[CRMActivity], int]:
        """Return paginated activities for a specific deal."""

        c = cast(Any, CRMActivity).__table__.c
        stmt = select(CRMActivity).where(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )

        # This service powers the "Tasks" API; restrict to task-like activity types.
        if task_types:
            requested = {t for t in task_types if t}
            allowed = requested & _TASK_ACTIVITY_TYPES
            # If client provided only non-task types, return an empty page rather than
            # accidentally returning non-task activities.
            if not allowed:
                return [], 0
            stmt = stmt.where(c.activity_type.in_(list(allowed)))
        else:
            stmt = stmt.where(c.activity_type.in_(list(_TASK_ACTIVITY_TYPES)))

        if statuses:
            stmt = stmt.where(c.status.in_(list(statuses)))

        if priorities:
            stmt = stmt.where(c.priority.in_(list(priorities)))

        if page < 1:
            raise TasksAccessServiceError("Page must be >= 1", status_code=400)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_count = await self.org_session.scalar(count_stmt) or 0

        offset = (page - 1) * limit
        stmt = stmt.order_by(c.created_at.desc(), c.activity_id.desc()).offset(offset).limit(limit)

        res = await self.org_session.execute(stmt)
        rows: list[CRMActivity] = list(res.scalars().all())

        return rows, total_count

    async def get_task_by_id(self, task_id: UUID) -> CRMActivity | None:
        """Fetch a single activity by ID (backwards compatible with task_id param name)."""

        try:
            c = cast(Any, CRMActivity).__table__.c
            stmt = select(CRMActivity).where(
                c.activity_id == task_id,
                c.organisation_id == self.organisation_id,
                or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
                c.activity_type.in_(list(_TASK_ACTIVITY_TYPES)),
            )
            res = await self.org_session.execute(stmt)
            return res.scalar_one_or_none()
        except Exception as exc:
            logger.error("Failed to get task %s: %s", task_id, exc, exc_info=True)
            raise TasksAccessServiceError("Failed to retrieve task") from exc
