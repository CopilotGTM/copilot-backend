"""CRM service layer for accessing deal tasks."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deal_tasks import CRMDealTask
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
    ) -> tuple[list[CRMDealTask], str | None]:
        """Return paginated tasks for a specific deal."""

        c = cast(Any, CRMDealTask).__table__.c
        stmt = select(CRMDealTask).where(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            c.is_deleted.is_(False),
            or_(c.is_deleted_remote.is_(False), c.is_deleted_remote.is_(None)),
        )

        if statuses:
            stmt = stmt.where(c.status.in_(list(statuses)))

        if priorities:
            stmt = stmt.where(c.priority.in_(list(priorities)))

        if task_types:
            stmt = stmt.where(c.task_type.in_(list(task_types)))

        if cursor:
            try:
                ts, task_id = decode_cursor(cursor)
            except Exception as exc:
                raise TasksAccessServiceError("Invalid cursor", status_code=400) from exc

            stmt = stmt.where(
                or_(
                    c.created_at < ts,
                    and_(c.created_at == ts, c.task_id < task_id),
                )
            )

        stmt = stmt.order_by(c.created_at.desc(), c.task_id.desc()).limit(limit + 1)

        res = await self.org_session.execute(stmt)
        rows: list[CRMDealTask] = list(res.scalars().all())

        has_more = len(rows) == limit + 1
        items = rows[:limit]

        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.created_at, last.task_id)

        return items, next_cursor

    async def get_task_by_id(self, task_id: UUID) -> CRMDealTask | None:
        """Fetch a single task by task ID."""

        try:
            c = cast(Any, CRMDealTask).__table__.c
            stmt = select(CRMDealTask).where(
                c.task_id == task_id,
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
            )
            res = await self.org_session.execute(stmt)
            return res.scalar_one_or_none()
        except Exception as exc:
            logger.error("Failed to get task %s: %s", task_id, exc, exc_info=True)
            raise TasksAccessServiceError("Failed to retrieve task") from exc

    async def get_tasks_grouped_by_status(
        self, deal_id: UUID
    ) -> dict[str, list[CRMDealTask] | float]:
        """Return tasks grouped into NEW/PENDING/COMPLETED buckets."""

        c = cast(Any, CRMDealTask).__table__.c
        stmt = (
            select(CRMDealTask)
            .where(
                c.organisation_id == self.organisation_id,
                c.deal_id == deal_id,
                c.is_deleted.is_(False),
                or_(c.is_deleted_remote.is_(False), c.is_deleted_remote.is_(None)),
            )
            .order_by(c.due_date.asc().nulls_last(), c.created_at.desc())
        )

        res = await self.org_session.execute(stmt)
        tasks: list[CRMDealTask] = list(res.scalars().all())

        new_tasks: list[CRMDealTask] = []
        pending_tasks: list[CRMDealTask] = []
        completed_tasks: list[CRMDealTask] = []

        for task in tasks:
            if task.status == "COMPLETED":
                completed_tasks.append(task)
            elif task.status in ("NOT_STARTED", None):
                new_tasks.append(task)
            else:
                pending_tasks.append(task)

        total = len(tasks)
        progress = (len(completed_tasks) / total * 100) if total else 0.0

        return {
            "new": new_tasks,
            "pending": pending_tasks,
            "completed": completed_tasks,
            "progress_percentage": round(progress, 1),
        }

    async def get_tasks_stats(self, deal_id: UUID) -> dict[str, Any]:
        """Aggregate statistics for tasks belonging to a deal."""

        c = cast(Any, CRMDealTask).__table__.c
        base_where = [
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            c.is_deleted.is_(False),
            or_(c.is_deleted_remote.is_(False), c.is_deleted_remote.is_(None)),
        ]

        total_stmt = select(func.count(c.task_id)).where(*base_where)
        total_res = await self.org_session.execute(total_stmt)
        total = total_res.scalar() or 0

        status_stmt = select(c.status, func.count(c.task_id)).where(*base_where).group_by(c.status)
        status_res = await self.org_session.execute(status_stmt)
        by_status = {row[0] or "UNKNOWN": row[1] for row in status_res.all()}

        priority_stmt = (
            select(c.priority, func.count(c.task_id)).where(*base_where).group_by(c.priority)
        )
        priority_res = await self.org_session.execute(priority_stmt)
        by_priority = {row[0] or "NONE": row[1] for row in priority_res.all()}

        type_stmt = (
            select(c.task_type, func.count(c.task_id)).where(*base_where).group_by(c.task_type)
        )
        type_res = await self.org_session.execute(type_stmt)
        by_type = {row[0] or "TODO": row[1] for row in type_res.all()}

        now = datetime.now(UTC)
        today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        today_end = today_start + timedelta(days=1)
        week_end = today_start + timedelta(days=7)

        overdue_stmt = select(func.count(c.task_id)).where(
            *base_where,
            c.status != "COMPLETED",
            c.due_date.is_not(None),
            c.due_date < now,
        )
        overdue_res = await self.org_session.execute(overdue_stmt)
        overdue_count = overdue_res.scalar() or 0

        due_today_stmt = select(func.count(c.task_id)).where(
            *base_where,
            c.status != "COMPLETED",
            c.due_date.is_not(None),
            c.due_date >= today_start,
            c.due_date < today_end,
        )
        due_today_res = await self.org_session.execute(due_today_stmt)
        due_today_count = due_today_res.scalar() or 0

        due_week_stmt = select(func.count(c.task_id)).where(
            *base_where,
            c.status != "COMPLETED",
            c.due_date.is_not(None),
            c.due_date >= today_start,
            c.due_date < week_end,
        )
        due_week_res = await self.org_session.execute(due_week_stmt)
        due_this_week_count = due_week_res.scalar() or 0

        completed_total = by_status.get("COMPLETED", 0)
        completed_percentage = (completed_total / total * 100) if total else 0.0

        return {
            "total": total,
            "by_status": by_status,
            "by_priority": by_priority,
            "by_type": by_type,
            "overdue_count": overdue_count,
            "due_today_count": due_today_count,
            "due_this_week_count": due_this_week_count,
            "completed_percentage": round(completed_percentage, 1),
        }
