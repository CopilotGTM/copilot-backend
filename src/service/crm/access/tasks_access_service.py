"""CRM service layer for accessing activities (tasks, calls, meetings, etc.)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityStatus
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

    async def list_tasks(
        self,
        *,
        deal_id: UUID | None = None,
        statuses: Iterable[str] | None = None,
        priorities: Iterable[str] | None = None,
        source: Iterable[str] | None = None,
        owner_user_id: UUID | None = None,
        created_by_user_id: UUID | None = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        is_overdue: bool | None = None,
        has_due_date: bool | None = None,
        include_completed: bool = False,
        sort_by: str = "due_date",
        sort_order: str = "asc",
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[CRMActivity], int]:
        """Global task listing with rich filtering for the Tasks page."""

        c = cast(Any, CRMActivity).__table__.c
        stmt = select(CRMActivity).where(
            c.organisation_id == self.organisation_id,
            c.activity_type.in_(list(_TASK_ACTIVITY_TYPES)),
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )

        if deal_id is not None:
            stmt = stmt.where(c.deal_id == deal_id)

        if statuses:
            stmt = stmt.where(c.status.in_(list(statuses)))
        elif not include_completed:
            # Exclude both terminal states by default
            stmt = stmt.where(
                c.status.notin_([
                    CRMActivityStatus.COMPLETED.value,
                    CRMActivityStatus.CANCELLED.value,
                ])
            )

        if priorities:
            stmt = stmt.where(c.priority.in_(list(priorities)))

        if source:
            stmt = stmt.where(c.source.in_(list(source)))

        if owner_user_id is not None:
            stmt = stmt.where(c.owner_external_id == str(owner_user_id))

        if created_by_user_id is not None:
            stmt = stmt.where(c.created_by_user_id == created_by_user_id)

        if due_from is not None:
            stmt = stmt.where(c.due_date >= due_from)

        if due_to is not None:
            stmt = stmt.where(c.due_date <= due_to)

        if has_due_date is True:
            stmt = stmt.where(c.due_date.isnot(None))
        elif has_due_date is False:
            stmt = stmt.where(c.due_date.is_(None))

        now = datetime.now(UTC)
        if is_overdue is True:
            stmt = stmt.where(
                c.due_date < now,
                c.status != CRMActivityStatus.COMPLETED.value,
            )
        elif is_overdue is False:
            stmt = stmt.where(
                or_(c.due_date.is_(None), c.due_date >= now)
            )

        if page < 1:
            raise TasksAccessServiceError("Page must be >= 1", status_code=400)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_count = await self.org_session.scalar(count_stmt) or 0

        # Sorting
        sort_col_map = {
            "due_date": c.due_date,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
            "priority": c.priority,
            "status": c.status,
            "subject": c.subject,
        }
        sort_col = sort_col_map.get(sort_by, c.due_date)
        # Nulls last for due_date ascending (tasks without due dates go to end)
        if sort_by == "due_date" and sort_order == "asc":
            stmt = stmt.order_by(sort_col.asc().nullslast(), c.created_at.desc())
        elif sort_order == "desc":
            stmt = stmt.order_by(sort_col.desc().nullsfirst(), c.created_at.desc())
        else:
            stmt = stmt.order_by(sort_col.asc(), c.created_at.desc())

        offset = (page - 1) * limit
        stmt = stmt.offset(offset).limit(limit)

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
