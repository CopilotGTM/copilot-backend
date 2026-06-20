"""Task read service for CRM task activities."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityStatus, CRMUser
from src.infra.utils.logger import get_logger
from src.service.crm.tasks.contracts import TaskReadServiceError

logger = get_logger("task_read_service")

_TASK_ACTIVITY_TYPES: set[str] = {"TASK", "TODO"}


def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _task_assignee_filter_key(task: Any) -> str | None:
    external_email = _normalize_email(getattr(task, "external_assignee_email", None))
    external_name = getattr(task, "external_assignee_name", None)
    if external_email:
        return f"email:{external_email}"
    if external_name:
        return f"external_name:{str(external_name).strip().lower()}"

    owner_email = _normalize_email(getattr(task, "owner_email", None))
    owner_id = getattr(task, "owner_id", None)
    properties = getattr(task, "properties", None) or {}
    public_user_id = properties.get("assigned_to_public_user_id")

    if owner_email:
        return f"email:{owner_email}"
    if public_user_id:
        return f"public_user:{public_user_id}"
    if owner_id:
        return f"crm_user:{owner_id}"
    owner_name = getattr(task, "owner_name", None)
    if owner_name:
        return f"name:{str(owner_name).strip().lower()}"
    return None


class TaskReadService:
    """Service for retrieving CRM deal tasks with filtering and pagination."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("TaskReadService close exception", exc_info=True)

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
            raise TaskReadServiceError("Page must be >= 1", status_code=400)

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
                c.status.notin_(
                    [
                        CRMActivityStatus.COMPLETED.value,
                        CRMActivityStatus.CANCELLED.value,
                    ]
                )
            )

        if priorities:
            stmt = stmt.where(c.priority.in_(list(priorities)))

        if source:
            stmt = stmt.where(c.source.in_(list(source)))

        if owner_user_id is not None:
            owner_value = str(owner_user_id)
            stmt = stmt.where(
                and_(
                    self._has_no_external_assignee_condition(),
                    or_(
                        c.owner_id == owner_user_id,
                        c.properties["assigned_to_public_user_id"].as_string() == owner_value,
                    ),
                )
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
            stmt = stmt.where(or_(c.due_date.is_(None), c.due_date >= now))

        if page < 1:
            raise TaskReadServiceError("Page must be >= 1", status_code=400)

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

    def _assigned_to_key_condition(self, assigned_to_key: str):
        c = cast(Any, CRMActivity).__table__.c
        prefix, _, raw_value = assigned_to_key.partition(":")
        value = raw_value.strip()
        lowered = value.lower()

        if not prefix or not value:
            return c.activity_id.is_(None)

        if prefix == "email":
            return or_(
                func.lower(c.external_assignee_email) == lowered,
                and_(
                    self._has_no_external_assignee_condition(),
                    func.lower(c.owner_email) == lowered,
                ),
            )

        if prefix == "public_user":
            return and_(
                self._has_no_external_assignee_condition(),
                c.properties["assigned_to_public_user_id"].as_string() == value,
            )

        if prefix == "crm_user":
            try:
                return and_(self._has_no_external_assignee_condition(), c.owner_id == UUID(value))
            except ValueError:
                return c.activity_id.is_(None)

        if prefix in {"name", "external_name"}:
            if prefix == "external_name":
                return func.lower(c.external_assignee_name) == lowered
            return and_(
                self._has_no_external_assignee_condition(), func.lower(c.owner_name) == lowered
            )

        return c.activity_id.is_(None)

    def _has_no_external_assignee_condition(self):
        c = cast(Any, CRMActivity).__table__.c
        return and_(c.external_assignee_email.is_(None), c.external_assignee_name.is_(None))

    def _current_user_assignee_condition(self, *, user_id: UUID, user_email: str | None):
        c = cast(Any, CRMActivity).__table__.c
        crm_user_columns = cast(Any, CRMUser).__table__.c

        internal_conditions = [
            c.owner_id == user_id,
            c.properties["assigned_to_public_user_id"].as_string() == str(user_id),
        ]
        if user_email:
            email_lower = user_email.lower()
            internal_conditions.append(func.lower(c.owner_email) == email_lower)
            crm_user_conditions = or_(
                crm_user_columns.user_id == user_id,
                func.lower(crm_user_columns.email) == email_lower,
            )
            internal_conditions.append(
                c.owner_id.in_(
                    select(crm_user_columns.user_id).where(
                        crm_user_columns.organisation_id == self.organisation_id,
                        crm_user_columns.is_deleted.is_(False),
                        crm_user_conditions,
                    )
                )
            )

        conditions = [
            and_(
                self._has_no_external_assignee_condition(),
                or_(*internal_conditions),
            )
        ]
        if user_email:
            conditions.append(func.lower(c.external_assignee_email) == user_email.lower())

        return or_(*conditions)

    async def resolve_current_user_task_assignees(
        self,
        *,
        user_id: UUID,
        user_email: str | None,
    ) -> list[dict[str, Any]]:
        c = cast(Any, CRMActivity).__table__.c
        base_conditions = [
            c.organisation_id == self.organisation_id,
            c.activity_type.in_(list(_TASK_ACTIVITY_TYPES)),
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
            select(func.count()).select_from(CRMActivity).where(*conditions)
        )
        return int(count or 0)

    async def list_task_assignees(
        self,
        *,
        q: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        c = cast(Any, CRMActivity).__table__.c
        base_conditions = [
            c.organisation_id == self.organisation_id,
            c.activity_type.in_(list(_TASK_ACTIVITY_TYPES)),
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        ]

        search_filter = None
        if q:
            like_expr = f"%{q.lower()}%"
            search_filter = or_(
                c.owner_name.ilike(like_expr),
                c.owner_email.ilike(like_expr),
                c.external_assignee_name.ilike(like_expr),
                c.external_assignee_email.ilike(like_expr),
            )

        owner_stmt = (
            select(
                c.owner_id,
                func.max(c.owner_name).label("owner_name"),
                func.max(c.owner_email).label("owner_email"),
                func.count().label("task_count"),
            )
            .where(
                *base_conditions,
                self._has_no_external_assignee_condition(),
                c.owner_id.is_not(None),
            )
            .group_by(c.owner_id)
            .order_by(func.max(c.owner_name).asc().nulls_last(), c.owner_id.asc())
            .limit(limit)
        )
        owner_email_stmt = (
            select(
                c.owner_email,
                func.max(c.owner_name).label("owner_name"),
                func.count().label("task_count"),
            )
            .where(
                *base_conditions,
                self._has_no_external_assignee_condition(),
                c.owner_id.is_(None),
                c.owner_email.is_not(None),
            )
            .group_by(c.owner_email)
            .order_by(func.max(c.owner_name).asc().nulls_last(), c.owner_email.asc())
            .limit(limit)
        )
        external_stmt = (
            select(
                c.external_assignee_email,
                func.max(c.external_assignee_name).label("external_assignee_name"),
                func.count().label("task_count"),
            )
            .where(
                *base_conditions,
                or_(
                    c.external_assignee_email.is_not(None),
                    c.external_assignee_name.is_not(None),
                ),
            )
            .group_by(c.external_assignee_email, c.external_assignee_name)
            .order_by(
                func.max(c.external_assignee_name).asc().nulls_last(),
                c.external_assignee_email.asc().nulls_last(),
            )
            .limit(limit)
        )

        if search_filter is not None:
            owner_stmt = owner_stmt.where(search_filter)
            owner_email_stmt = owner_email_stmt.where(search_filter)
            external_stmt = external_stmt.where(search_filter)

        grouped: dict[str, dict[str, Any]] = {}
        owner_result = await self.org_session.execute(owner_stmt)
        for row in owner_result.all():
            task = SimpleNamespace(
                owner_id=row.owner_id,
                owner_name=row.owner_name,
                owner_email=row.owner_email,
                external_assignee_email=None,
                external_assignee_name=None,
                properties={},
            )
            filter_key = _task_assignee_filter_key(task)
            if not filter_key:
                continue
            grouped.setdefault(filter_key, {"task": task, "task_count": 0})
            grouped[filter_key]["task_count"] += row.task_count

        owner_email_result = await self.org_session.execute(owner_email_stmt)
        for row in owner_email_result.all():
            task = SimpleNamespace(
                owner_id=None,
                owner_name=row.owner_name,
                owner_email=row.owner_email,
                external_assignee_email=None,
                external_assignee_name=None,
                properties={},
            )
            filter_key = _task_assignee_filter_key(task)
            if not filter_key:
                continue
            grouped.setdefault(filter_key, {"task": task, "task_count": 0})
            grouped[filter_key]["task_count"] += row.task_count

        external_result = await self.org_session.execute(external_stmt)
        for row in external_result.all():
            task = SimpleNamespace(
                owner_id=None,
                owner_name=None,
                owner_email=None,
                external_assignee_email=row.external_assignee_email,
                external_assignee_name=row.external_assignee_name,
                properties={},
            )
            filter_key = _task_assignee_filter_key(task)
            if not filter_key:
                continue
            grouped.setdefault(filter_key, {"task": task, "task_count": 0})
            grouped[filter_key]["task_count"] += row.task_count

        return sorted(
            grouped.values(),
            key=lambda item: (
                (
                    getattr(item["task"], "external_assignee_name", None)
                    or getattr(item["task"], "owner_name", None)
                    or getattr(item["task"], "external_assignee_email", None)
                    or getattr(item["task"], "owner_email", None)
                    or ""
                ).lower(),
                _task_assignee_filter_key(item["task"]) or "",
            ),
        )[:limit]

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
            raise TaskReadServiceError("Failed to retrieve task") from exc
