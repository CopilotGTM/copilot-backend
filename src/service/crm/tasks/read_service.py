"""Task read service for CRM task source rows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import DateTime, and_, case, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.reminder_models import ReminderStatus, UserReminder
from src.domain.models.organisation_tables.sync.crm import CRMTask, CRMUser
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMRecordAccessError,
    DealAccessPolicy,
)
from src.service.crm.tasks.contracts import (
    TaskReadRecord,
    TaskReadServiceError,
    TaskReminderSummary,
)

logger = get_logger("task_read_service")

_TASK_ACTIVITY_TYPES: set[str] = {"TASK", "TODO"}
_TERMINAL_STATUSES = {"COMPLETED", "CANCELLED"}


def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _task_type(task: Any) -> str:
    metadata = getattr(task, "task_metadata", None) or {}
    task_type = metadata.get("task_type")
    return task_type if task_type in _TASK_ACTIVITY_TYPES else "TASK"


def _task_assignee_filter_key(task: Any) -> str | None:
    metadata = getattr(task, "task_metadata", None) or {}
    email = _normalize_email(getattr(task, "assigned_to_email", None))
    name = getattr(task, "assigned_to_name", None)
    public_user_id = metadata.get("assigned_to_public_user_id")
    crm_user_id = getattr(task, "assigned_to_user_id", None)

    if metadata.get("assignee_kind") == "external":
        if email:
            return f"email:{email}"
        if name:
            return f"external_name:{str(name).strip().lower()}"
        return None

    if email:
        return f"email:{email}"
    if public_user_id:
        return f"public_user:{public_user_id}"
    if crm_user_id:
        return f"crm_user:{crm_user_id}"
    if name:
        return f"name:{str(name).strip().lower()}"
    return None


def _assignee_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    task = row["task"]
    return (
        str(getattr(task, "assigned_to_name", "") or "").lower(),
        str(getattr(task, "assigned_to_email", "") or "").lower(),
        _task_assignee_filter_key(task) or "",
    )


def _prefer_assignee_display_task(current: Any, candidate: Any) -> Any:
    current_score = (
        bool(getattr(current, "assigned_to_name", None)),
        bool(getattr(current, "assigned_to_email", None)),
        bool(getattr(current, "assigned_to_user_id", None)),
    )
    candidate_score = (
        bool(getattr(candidate, "assigned_to_name", None)),
        bool(getattr(candidate, "assigned_to_email", None)),
        bool(getattr(candidate, "assigned_to_user_id", None)),
    )
    return candidate if candidate_score > current_score else current


def _merge_assignee_rows(rows: Sequence[Any], *, limit: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        task = SimpleNamespace(
            assigned_to_user_id=row.assigned_to_user_id,
            assigned_to_name=row.assigned_to_name,
            assigned_to_email=row.assigned_to_email,
            task_metadata={"assignee_kind": row.assignee_kind} if row.assignee_kind else {},
        )
        filter_key = _task_assignee_filter_key(task)
        if not filter_key:
            continue

        task_count = int(row.task_count or 0)
        existing = merged.get(filter_key)
        if existing is None:
            merged[filter_key] = {"task": task, "task_count": task_count}
            continue

        existing["task_count"] += task_count
        existing["task"] = _prefer_assignee_display_task(existing["task"], task)

    return sorted(merged.values(), key=_assignee_sort_key)[:limit]


class TaskReadService:
    """Service for retrieving CRM tasks with filtering and pagination."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        access_actor: CRMAccessActor | None = None,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.access_actor = access_actor
        self._access = DealAccessPolicy(org_session, organisation_id, access_actor)

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("TaskReadService close exception", exc_info=True)

    @property
    def session(self) -> AsyncSession:
        """Expose the caller-owned organisation session for compatibility facades."""
        return self.org_session

    async def list_tasks_by_deal(
        self,
        deal_id: UUID,
        limit: int = 50,
        page: int = 1,
        statuses: Iterable[str] | None = None,
        priorities: Iterable[str] | None = None,
        task_types: Iterable[str] | None = None,
    ) -> tuple[list[TaskReadRecord], int]:
        """Return paginated source tasks for a specific deal."""
        if page < 1:
            raise TaskReadServiceError("Page must be >= 1", status_code=400)
        if self.access_actor is not None:
            try:
                await self._access.require_deal(deal_id, "view")
            except CRMRecordAccessError as exc:
                raise TaskReadServiceError(exc.message, exc.status_code) from exc

        c = cast(Any, CRMTask).__table__.c
        stmt = self._select_tasks_with_deal_name().where(
            c.organisation_id == self.organisation_id,
            self._visible_task_condition(),
            c.deal_id == deal_id,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )

        if task_types:
            requested = {t for t in task_types if t}
            allowed = requested & _TASK_ACTIVITY_TYPES
            if not allowed:
                return [], 0
            stmt = stmt.where(c.task_metadata["task_type"].as_string().in_(list(allowed)))

        if statuses:
            stmt = stmt.where(c.status.in_(list(statuses)))

        if priorities:
            stmt = stmt.where(c.priority.in_(list(priorities)))

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_count = await self.org_session.scalar(count_stmt) or 0

        offset = (page - 1) * limit
        stmt = stmt.order_by(c.created_at.desc(), c.task_id.desc()).offset(offset).limit(limit)
        result = await self.org_session.execute(stmt)
        records = self._read_records(result.all())
        return await self._attach_reminder_summaries(records), total_count

    async def list_tasks(
        self,
        *,
        scope: str = "assigned_to_me",
        current_user_id: UUID | None = None,
        current_user_email: str | None = None,
        deal_id: UUID | None = None,
        statuses: Iterable[str] | None = None,
        priorities: Iterable[str] | None = None,
        source: Iterable[str] | None = None,
        owner_user_id: UUID | None = None,
        assigned_to_key: str | None = None,
        created_by_user_id: UUID | None = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        due_local_date: date | None = None,
        is_overdue: bool | None = None,
        has_due_date: bool | None = None,
        include_completed: bool = False,
        q: str | None = None,
        sort_by: str = "due_date",
        sort_order: str = "asc",
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[TaskReadRecord], int]:
        """Global task listing with rich filtering for the Tasks page."""
        if page < 1:
            raise TaskReadServiceError("Page must be >= 1", status_code=400)

        search_query = q.strip() if q else ""
        if search_query and len(search_query) < 2:
            raise TaskReadServiceError(
                "Search query must contain at least 2 characters",
                status_code=400,
            )
        if len(search_query) > 100:
            raise TaskReadServiceError(
                "Search query must contain at most 100 characters",
                status_code=400,
            )

        c = cast(Any, CRMTask).__table__.c
        stmt = self._select_tasks_with_deal_name().where(
            c.organisation_id == self.organisation_id,
            self._visible_task_condition(),
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )

        if deal_id is not None:
            stmt = stmt.where(c.deal_id == deal_id)

        if statuses:
            stmt = stmt.where(c.status.in_(list(statuses)))
        elif not include_completed:
            stmt = stmt.where(c.status.notin_(list(_TERMINAL_STATUSES)))

        if priorities:
            stmt = stmt.where(c.priority.in_(list(priorities)))

        if source:
            stmt = stmt.where(c.task_metadata["source"].as_string().in_(list(source)))

        if owner_user_id is not None:
            assignee_keys = await self.resolve_current_user_task_assignees(
                user_id=owner_user_id,
                user_email=None,
            )
            if not assignee_keys:
                return [], 0
            stmt = stmt.where(
                or_(*[self._assigned_to_key_condition(row["filter_key"]) for row in assignee_keys])
            )

        if assigned_to_key:
            stmt = stmt.where(self._assigned_to_key_condition(assigned_to_key))
        elif owner_user_id is None:
            if scope == "assigned_to_me":
                if current_user_id is None:
                    return [], 0
                stmt = stmt.where(
                    self._current_user_assignee_condition(
                        user_id=current_user_id,
                        user_email=current_user_email,
                    )
                )
            elif scope == "assigned_by_me":
                if current_user_id is None:
                    return [], 0
                stmt = stmt.where(c.created_by_user_id == current_user_id)
            elif scope == "assigned_to_user":
                raise TaskReadServiceError(
                    "assigned_to_key is required when scope=assigned_to_user",
                    status_code=400,
                )
            else:
                raise TaskReadServiceError("Invalid task scope", status_code=400)

        if created_by_user_id is not None:
            stmt = stmt.where(c.created_by_user_id == created_by_user_id)

        due_expr = func.coalesce(c.due_at, sql_cast(c.due_date, DateTime(timezone=True)))
        if due_local_date is not None:
            # Timed tasks are UTC instants, while date-only tasks are already a
            # local calendar date. Coalescing them would shift "today" by timezone.
            timed_conditions = [c.due_at.isnot(None)]
            if due_from is not None:
                timed_conditions.append(c.due_at >= due_from)
            if due_to is not None:
                timed_conditions.append(c.due_at <= due_to)
            stmt = stmt.where(
                or_(
                    and_(*timed_conditions),
                    and_(c.due_at.is_(None), c.due_date == due_local_date),
                )
            )
        else:
            if due_from is not None:
                stmt = stmt.where(due_expr >= due_from)

            if due_to is not None:
                stmt = stmt.where(due_expr <= due_to)

        if has_due_date is True:
            stmt = stmt.where(or_(c.due_at.isnot(None), c.due_date.isnot(None)))
        elif has_due_date is False:
            stmt = stmt.where(c.due_at.is_(None), c.due_date.is_(None))

        now = datetime.now(UTC)
        if is_overdue is True:
            stmt = stmt.where(due_expr < now, c.status != "COMPLETED")
        elif is_overdue is False:
            stmt = stmt.where(or_(due_expr.is_(None), due_expr >= now))

        if search_query:
            like_expr = f"%{search_query}%"
            deal_c = cast(Any, CRMDeal).__table__.c
            stmt = stmt.where(
                or_(
                    c.title.ilike(like_expr),
                    c.description.ilike(like_expr),
                    deal_c.name.ilike(like_expr),
                    c.assigned_to_name.ilike(like_expr),
                    c.assigned_to_email.ilike(like_expr),
                    c.owner_email.ilike(like_expr),
                    c.task_metadata["source"].as_string().ilike(like_expr),
                    c.priority.ilike(like_expr),
                    c.status.ilike(like_expr),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_count = await self.org_session.scalar(count_stmt) or 0

        priority_order = case(
            (c.priority == "NONE", 0),
            (c.priority == "LOW", 1),
            (c.priority == "MEDIUM", 2),
            (c.priority == "HIGH", 3),
            (c.priority == "URGENT", 4),
            else_=5,
        )
        status_order = case(
            (c.status == "NOT_STARTED", 0),
            (c.status == "IN_PROGRESS", 1),
            (c.status == "COMPLETED", 2),
            (c.status == "CANCELLED", 3),
            else_=4,
        )
        sort_col_map = {
            "due_date": due_expr,
            "created_at": c.created_at,
            "priority": priority_order,
            "status": status_order,
            "subject": func.lower(c.title),
            "assignee": func.lower(func.coalesce(c.assigned_to_name, c.assigned_to_email)),
        }
        if sort_by not in sort_col_map:
            raise TaskReadServiceError("Unsupported sort field", status_code=400)
        if sort_order not in {"asc", "desc"}:
            raise TaskReadServiceError("sort_order must be asc or desc", status_code=400)

        sort_col = sort_col_map[sort_by]
        primary_order = (
            sort_col.asc().nullslast() if sort_order == "asc" else sort_col.desc().nullslast()
        )
        stmt = stmt.order_by(primary_order, c.task_id.asc())

        offset = (page - 1) * limit
        stmt = stmt.offset(offset).limit(limit)

        result = await self.org_session.execute(stmt)
        records = self._read_records(result.all())
        return await self._attach_reminder_summaries(records), total_count

    def _assigned_to_key_condition(self, assigned_to_key: str):
        c = cast(Any, CRMTask).__table__.c
        prefix, _, raw_value = assigned_to_key.partition(":")
        value = raw_value.strip()
        lowered = value.lower()

        if not prefix or not value:
            return c.task_id.is_(None)

        if prefix == "email":
            return func.lower(c.assigned_to_email) == lowered

        if prefix == "public_user":
            return and_(
                self._has_no_external_assignee_condition(),
                c.task_metadata["assigned_to_public_user_id"].as_string() == value,
            )

        if prefix == "crm_user":
            try:
                return and_(
                    self._has_no_external_assignee_condition(),
                    c.assigned_to_user_id == UUID(value),
                )
            except ValueError:
                return c.task_id.is_(None)

        if prefix in {"name", "external_name"}:
            return and_(
                self._external_assignee_condition()
                if prefix == "external_name"
                else self._has_no_external_assignee_condition(),
                func.lower(c.assigned_to_name) == lowered,
            )

        return c.task_id.is_(None)

    def _has_no_external_assignee_condition(self):
        c = cast(Any, CRMTask).__table__.c
        return or_(
            c.task_metadata["assignee_kind"].as_string().is_(None),
            c.task_metadata["assignee_kind"].as_string() != "external",
        )

    def _external_assignee_condition(self):
        c = cast(Any, CRMTask).__table__.c
        return c.task_metadata["assignee_kind"].as_string() == "external"

    def _current_user_assignee_condition(self, *, user_id: UUID, user_email: str | None):
        c = cast(Any, CRMTask).__table__.c
        crm_user_columns = cast(Any, CRMUser).__table__.c

        internal_conditions = [
            c.task_metadata["assigned_to_public_user_id"].as_string() == str(user_id),
        ]
        if user_email:
            email_lower = user_email.lower()
            internal_conditions.append(func.lower(c.assigned_to_email) == email_lower)
            crm_user_conditions = or_(
                crm_user_columns.user_id == user_id,
                func.lower(crm_user_columns.email) == email_lower,
            )
            internal_conditions.append(
                c.assigned_to_user_id.in_(
                    select(crm_user_columns.user_id).where(
                        crm_user_columns.organisation_id == self.organisation_id,
                        crm_user_columns.is_deleted.is_(False),
                        crm_user_conditions,
                    )
                )
            )

        return and_(
            self._has_no_external_assignee_condition(),
            or_(*internal_conditions),
        )

    async def resolve_current_user_task_assignees(
        self,
        *,
        user_id: UUID,
        user_email: str | None,
    ) -> list[dict[str, Any]]:
        """Return stored assignee identities that represent one authenticated user."""
        c = cast(Any, CRMTask).__table__.c
        base_conditions = [
            c.organisation_id == self.organisation_id,
            self._visible_task_condition(),
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        ]
        options: list[dict[str, Any]] = []

        if user_email:
            email = user_email.lower()
            count = await self._count_tasks_for_condition(
                *base_conditions,
                self._assigned_to_key_condition(f"email:{email}"),
            )
            if count:
                options.append(
                    {
                        "kind": "internal_user",
                        "filter_key": f"email:{email}",
                        "name": None,
                        "email": email,
                        "public_user_id": user_id,
                        "crm_user_id": None,
                        "task_count": count,
                    }
                )

        crm_user_ids = await self._resolve_current_user_crm_user_ids(
            user_id=user_id,
            user_email=user_email,
        )
        for crm_user_id in crm_user_ids:
            filter_key = f"crm_user:{crm_user_id}"
            count = await self._count_tasks_for_condition(
                *base_conditions,
                self._assigned_to_key_condition(filter_key),
            )
            if count:
                options.append(
                    {
                        "kind": "internal_user",
                        "filter_key": filter_key,
                        "name": None,
                        "email": user_email.lower() if user_email else None,
                        "public_user_id": user_id,
                        "crm_user_id": crm_user_id,
                        "task_count": count,
                    }
                )

        if not options:
            filter_key = f"public_user:{user_id}"
            count = await self._count_tasks_for_condition(
                *base_conditions,
                self._assigned_to_key_condition(filter_key),
            )
            if count:
                options.append(
                    {
                        "kind": "internal_user",
                        "filter_key": filter_key,
                        "name": None,
                        "email": user_email.lower() if user_email else None,
                        "public_user_id": user_id,
                        "crm_user_id": None,
                        "task_count": count,
                    }
                )

        return options

    async def _resolve_current_user_crm_user_ids(
        self,
        *,
        user_id: UUID,
        user_email: str | None,
    ) -> list[UUID]:
        crm_user_columns = cast(Any, CRMUser).__table__.c
        conditions = [crm_user_columns.user_id == user_id]
        if user_email:
            conditions.append(func.lower(crm_user_columns.email) == user_email.lower())

        result = await self.org_session.execute(
            select(crm_user_columns.user_id)
            .where(
                crm_user_columns.organisation_id == self.organisation_id,
                crm_user_columns.is_deleted.is_(False),
                or_(*conditions),
            )
            .order_by(crm_user_columns.user_id)
        )
        return [cast(UUID, row[0]) for row in result.all()]

    async def _count_tasks_for_condition(self, *conditions) -> int:
        count = await self.org_session.scalar(
            select(func.count()).select_from(CRMTask).where(*conditions)
        )
        return int(count or 0)

    async def list_task_assignees(
        self,
        *,
        q: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List visible task assignees with merged identity keys and task counts."""
        c = cast(Any, CRMTask).__table__.c
        assignee_kind = c.task_metadata["assignee_kind"].as_string()
        base_conditions = [
            c.organisation_id == self.organisation_id,
            self._visible_task_condition(),
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        ]

        if q:
            like_expr = f"%{q.lower()}%"
            base_conditions.append(
                or_(c.assigned_to_name.ilike(like_expr), c.assigned_to_email.ilike(like_expr))
            )

        stmt = (
            select(
                c.assigned_to_user_id,
                c.assigned_to_name,
                c.assigned_to_email,
                assignee_kind.label("assignee_kind"),
                func.count().label("task_count"),
            )
            .where(*base_conditions)
            .group_by(
                c.assigned_to_user_id,
                c.assigned_to_name,
                c.assigned_to_email,
                assignee_kind,
            )
            .order_by(c.assigned_to_name.asc().nulls_last(), c.assigned_to_email.asc())
        )
        result = await self.org_session.execute(stmt)
        return _merge_assignee_rows(result.all(), limit=limit)

    async def get_task_by_id(self, task_id: UUID) -> TaskReadRecord | None:
        """Fetch a single task by ID."""
        try:
            c = cast(Any, CRMTask).__table__.c
            stmt = self._select_tasks_with_deal_name().where(
                c.task_id == task_id,
                c.organisation_id == self.organisation_id,
                self._visible_task_condition(),
                or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
            )
            result = await self.org_session.execute(stmt)
            rows = self._read_records(result.all())
            records = await self._attach_reminder_summaries(rows)
            record = records[0] if records else None
            if record and record.task.deal_id and self.access_actor is not None:
                try:
                    await self._access.require_deal(record.task.deal_id, "view")
                except CRMRecordAccessError:
                    return None
            return record
        except Exception as exc:
            logger.error("Failed to get task %s: %s", task_id, exc, exc_info=True)
            raise TaskReadServiceError("Failed to retrieve task") from exc

    def _visible_task_condition(self):
        """Allow accessible-deal tasks plus records directly owned by the actor."""
        task = cast(Any, CRMTask).__table__.c
        actor = self.access_actor
        if actor is None or actor.is_admin:
            return task.organisation_id == self.organisation_id

        deal = cast(Any, CRMDeal).__table__.c
        accessible_deals = select(deal.deal_id).where(
            deal.organisation_id == self.organisation_id,
            deal.is_deleted.is_(False),
            self._access.visible_deal_predicate(deal),
        )
        personal = [
            task.created_by_user_id == actor.user_id,
            task.assigned_to_user_id == actor.user_id,
            task.task_metadata["assigned_to_public_user_id"].as_string() == str(actor.user_id),
        ]
        if actor.normalized_email:
            personal.append(func.lower(task.assigned_to_email) == actor.normalized_email)
        # Assignment and creation are direct task grants even when the parent
        # deal is otherwise hidden. The outer query still enforces organisation
        # isolation, so this cannot expose another tenant's task.
        return or_(task.deal_id.in_(accessible_deals), *personal)

    def _select_tasks_with_deal_name(self):
        deal_c = cast(Any, CRMDeal).__table__.c
        task_c = cast(Any, CRMTask).__table__.c
        return (
            select(CRMTask)
            .add_columns(deal_c.name)
            .outerjoin(
                CRMDeal,
                and_(
                    deal_c.organisation_id == self.organisation_id,
                    deal_c.deal_id == task_c.deal_id,
                ),
            )
        )

    @staticmethod
    def _read_records(rows: Sequence[Any]) -> list[TaskReadRecord]:
        return [TaskReadRecord(task=row[0], deal_name=row[1]) for row in rows]

    async def _attach_reminder_summaries(
        self, records: Sequence[TaskReadRecord]
    ) -> list[TaskReadRecord]:
        if not records:
            return []

        task_ids = [record.task.task_id for record in records]
        summaries = await self._load_reminder_summaries(task_ids)
        return [
            TaskReadRecord(
                task=record.task,
                deal_name=record.deal_name,
                has_active_reminder=summaries.get(record.task.task_id, (False, None))[0],
                reminder=summaries.get(record.task.task_id, (False, None))[1],
            )
            for record in records
        ]

    async def _load_reminder_summaries(
        self, task_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[bool, TaskReminderSummary | None]]:
        if not task_ids:
            return {}

        stmt = (
            select(UserReminder)
            .where(
                col(UserReminder.organisation_id) == self.organisation_id,
                col(UserReminder.activity_id).in_(list(task_ids)),
                col(UserReminder.status).in_(
                    [
                        ReminderStatus.ACTIVE.value,
                        ReminderStatus.PAUSED.value,
                    ]
                ),
            )
            .order_by(
                col(UserReminder.status).asc(),
                col(UserReminder.next_trigger_at).asc(),
                col(UserReminder.reminder_id).asc(),
            )
        )
        result = await self.org_session.execute(stmt)
        summaries: dict[UUID, tuple[bool, TaskReminderSummary | None]] = {}
        for reminder in result.scalars().all():
            activity_id = reminder.activity_id
            if activity_id is None or activity_id in summaries:
                continue
            status_value = (
                reminder.status.value
                if isinstance(reminder.status, ReminderStatus)
                else str(reminder.status)
            )
            recurrence_value = getattr(reminder.recurrence, "value", reminder.recurrence)
            summaries[activity_id] = (
                status_value == ReminderStatus.ACTIVE.value,
                TaskReminderSummary(
                    reminder_id=reminder.reminder_id,
                    status=status_value,
                    trigger_time=reminder.next_trigger_at,
                    recurrence=str(recurrence_value) if recurrence_value is not None else None,
                    delivery_channel=reminder.delivery_channel,
                ),
            )
        return summaries
