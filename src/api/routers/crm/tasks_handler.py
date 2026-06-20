"""CRM Deal Tasks API handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_task_models import (
    CreateTaskRequest,
    TaskAssignee,
    TaskAssigneeOption,
    TaskAssigneesResponse,
    TaskDetail,
    TaskListItem,
    TaskOption,
    TaskOptionsResponse,
    TasksPage,
    UpdateTaskRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.reminder_models import (
    ReminderStatus,
    UserReminder,
)
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.tasks.contracts import TaskReadServiceError, TaskWriteServiceError
from src.service.crm.tasks.read_service import TaskReadService
from src.service.crm.tasks.write_service import TaskWriteService

logger = get_logger("tasks_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

tasks_router = APIRouter()

_TASK_STATUS_OPTIONS = [
    TaskOption(value=CRMActivityStatus.NOT_STARTED.value, label="To Do"),
    TaskOption(value=CRMActivityStatus.IN_PROGRESS.value, label="In Progress"),
    TaskOption(value=CRMActivityStatus.WAITING.value, label="Waiting"),
    TaskOption(value=CRMActivityStatus.DEFERRED.value, label="Deferred"),
    TaskOption(value=CRMActivityStatus.COMPLETED.value, label="Done"),
    TaskOption(value=CRMActivityStatus.CANCELLED.value, label="Cancelled"),
]
_TASK_PRIORITY_OPTIONS = [
    TaskOption(value=CRMActivityPriority.NONE.value, label="None"),
    TaskOption(value=CRMActivityPriority.LOW.value, label="Low"),
    TaskOption(value=CRMActivityPriority.MEDIUM.value, label="Medium"),
    TaskOption(value=CRMActivityPriority.HIGH.value, label="High"),
    TaskOption(value=CRMActivityPriority.URGENT.value, label="Urgent"),
]
_TASK_TYPE_OPTIONS = [
    TaskOption(value=CRMActivityType.TASK.value, label="Task"),
    TaskOption(value=CRMActivityType.TODO.value, label="To-do"),
]
_TASK_SOURCE_OPTIONS = [
    TaskOption(value=CRMActivitySource.MANUAL.value, label="Manual"),
    TaskOption(value=CRMActivitySource.CRM_SYNC.value, label="CRM"),
    TaskOption(value=CRMActivitySource.AI_MEETING.value, label="AI Meeting"),
    TaskOption(value=CRMActivitySource.AI_INSIGHT.value, label="AI Insight"),
]


def _compute_is_overdue(due_date: datetime | None, status: str | None) -> bool:
    """Return True when a task is past due and not in a terminal state."""

    if not due_date or status in ("COMPLETED", "CANCELLED"):
        return False

    reference = datetime.now(UTC)
    due = due_date if due_date.tzinfo else due_date.replace(tzinfo=UTC)
    return due < reference


def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _parse_uuid(value: str | UUID | None) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _build_task_assignee(task) -> TaskAssignee | None:
    """Return a stable assignee contract over CRM owner/external-assignee columns."""

    properties = getattr(task, "properties", None) or {}

    external_email = _normalize_email(getattr(task, "external_assignee_email", None))
    external_name = getattr(task, "external_assignee_name", None)
    if external_email or external_name:
        if external_email:
            filter_key = f"email:{external_email}"
        else:
            filter_key = f"external_name:{str(external_name).strip().lower()}"
        return TaskAssignee(
            kind="external",
            filter_key=filter_key,
            name=external_name,
            email=external_email,
        )

    owner_email = _normalize_email(getattr(task, "owner_email", None))
    owner_name = getattr(task, "owner_name", None)
    public_user_id = _parse_uuid(properties.get("assigned_to_public_user_id"))
    crm_user_id = _parse_uuid(getattr(task, "owner_id", None))

    if owner_email:
        filter_key = f"email:{owner_email}"
    elif public_user_id:
        filter_key = f"public_user:{public_user_id}"
    elif crm_user_id:
        filter_key = f"crm_user:{crm_user_id}"
    elif owner_name:
        filter_key = f"name:{str(owner_name).strip().lower()}"
    else:
        return None

    kind = "internal_user" if public_user_id else "crm_owner" if crm_user_id else "unknown"
    return TaskAssignee(
        kind=kind,
        filter_key=filter_key,
        name=owner_name,
        email=owner_email,
        public_user_id=public_user_id,
        crm_user_id=crm_user_id,
    )


def _build_task_due_metadata(task) -> dict[str, str | bool | None]:
    properties = getattr(task, "properties", None) or {}
    is_all_day = bool(getattr(task, "is_all_day", False))
    timezone_name = properties.get("due_timezone")
    due_local_date = properties.get("due_local_date")

    due_date = getattr(task, "due_date", None)
    if is_all_day and due_date is not None and timezone_name and not due_local_date:
        try:
            due = due_date if due_date.tzinfo else due_date.replace(tzinfo=UTC)
            due_local_date = due.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        except (ZoneInfoNotFoundError, ValueError):
            due_local_date = None

    return {
        "due_local_date": due_local_date if isinstance(due_local_date, str) else None,
        "due_timezone": timezone_name if isinstance(timezone_name, str) else None,
        "is_all_day": is_all_day,
    }


async def get_tasks_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a TasksAccessService instance for the current org."""

    service = TaskReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Tasks service close failed", exc_info=True)


async def get_tasks_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a TasksWriteService instance for the current org."""

    service = TaskWriteService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        user_timezone=user_ids.user_timezone,
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Tasks write service close failed", exc_info=True)


async def _enrich_task_list_item(task, session: AsyncSession) -> TaskListItem:
    """Build a TaskListItem with computed fields (is_overdue, has_reminder, deal_name)."""
    item = TaskListItem.model_validate(task, from_attributes=True)

    has_reminder = await _has_active_reminder(session, task.activity_id)
    deal_name = task.related_name if task.deal_id else None

    return item.model_copy(
        update={
            "is_overdue": _compute_is_overdue(task.due_date, task.status),
            "has_reminder": has_reminder,
            "deal_name": deal_name,
            "assigned_to": _build_task_assignee(task),
            **_build_task_due_metadata(task),
            # Mirrors the write-path guard in TaskWriteService._load_user_task:
            # integration_id is the authoritative CRM-synced predicate. Do NOT use
            # `source` here — it is nullable and can drift from integration_id.
            "is_read_only": task.integration_id is not None,
        }
    )


async def _enrich_task_detail(task, session: AsyncSession) -> TaskDetail:
    """Build a TaskDetail with computed fields."""
    detail = TaskDetail.model_validate(task, from_attributes=True)

    has_reminder = await _has_active_reminder(session, task.activity_id)
    deal_name = task.related_name if task.deal_id else None
    reminder_dict = await _get_reminder_dict(session, task.activity_id)

    return detail.model_copy(
        update={
            "is_overdue": _compute_is_overdue(task.due_date, task.status),
            "has_reminder": has_reminder,
            "deal_name": deal_name,
            "reminder": reminder_dict,
            "assigned_to": _build_task_assignee(task),
            **_build_task_due_metadata(task),
            # Mirrors the write-path guard in TaskWriteService._load_user_task:
            # integration_id is the authoritative CRM-synced predicate. Do NOT use
            # `source` here — it is nullable and can drift from integration_id.
            "is_read_only": task.integration_id is not None,
        }
    )


async def _has_active_reminder(session: AsyncSession, activity_id: UUID) -> bool:
    """Check if at least one active reminder is linked to this activity."""
    stmt = (
        select(func.count())
        .select_from(UserReminder)
        .where(
            col(UserReminder.activity_id) == activity_id,
            col(UserReminder.status) == ReminderStatus.ACTIVE.value,
        )
    )
    count = await session.scalar(stmt) or 0
    return count > 0


async def _get_reminder_dict(session: AsyncSession, activity_id: UUID) -> dict | None:
    """Get linked reminder details for task detail view."""
    stmt = (
        select(UserReminder)
        .where(
            col(UserReminder.activity_id) == activity_id,
            col(UserReminder.status).in_(
                [
                    ReminderStatus.ACTIVE.value,
                    ReminderStatus.PAUSED.value,
                ]
            ),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    reminder = result.scalar_one_or_none()
    if not reminder:
        return None
    return {
        "reminder_id": str(reminder.reminder_id),
        "status": reminder.status,
        "trigger_time": reminder.next_trigger_at.isoformat() if reminder.next_trigger_at else None,
        "recurrence": reminder.recurrence,
        "delivery_channel": reminder.delivery_channel,
    }


# ── Global task list ─────────────────────────────────────────────


@tasks_router.get(
    "/tasks",
    response_model=GenericResponse[TasksPage],
    tags=["crm", "tasks"],
    summary="List tasks across all deals or standalone",
    description="Global task listing with rich filtering for the Tasks page.",
)
async def list_tasks(
    response: FastAPIResponse,
    scope: Literal["assigned_to_me", "assigned_by_me", "assigned_to_user"] = Query(
        "assigned_to_me",
        description="Required task scope. Normal users cannot request all organisation tasks.",
    ),
    deal_id: UUID | None = Query(None, description="Filter by deal"),
    statuses: list[str] | None = Query(None, description="Filter by statuses"),
    priorities: list[str] | None = Query(None, description="Filter by priorities"),
    source: list[str] | None = Query(None, description="Filter by source"),
    owner_user_id: UUID | None = Query(None, description="Filter by assignee"),
    assigned_to_key: str | None = Query(
        None,
        description="Canonical assignee filter key returned by /tasks/assignees.",
    ),
    created_by_user_id: UUID | None = Query(None, description="Filter by creator"),
    due_from: datetime | None = Query(None, description="Due date range start"),
    due_to: datetime | None = Query(None, description="Due date range end"),
    is_overdue: bool | None = Query(None, description="Filter overdue tasks only"),
    has_due_date: bool | None = Query(None, description="Filter by has/no due date"),
    include_completed: bool = Query(False, description="Include completed tasks"),
    sort_by: str = Query("due_date", description="Sort field"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$", description="Sort direction"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    user_ids: UserIDs = Depends(get_current_user),
    service: TaskReadService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TasksPage]:
    """Global task listing with rich filtering."""

    try:
        tasks, total_count = await service.list_tasks(
            scope=scope,
            current_user_id=user_ids.user_id,
            current_user_email=user_ids.user_email,
            deal_id=deal_id,
            statuses=statuses,
            priorities=priorities,
            source=source,
            owner_user_id=owner_user_id,
            assigned_to_key=assigned_to_key,
            created_by_user_id=created_by_user_id,
            due_from=due_from,
            due_to=due_to,
            is_overdue=is_overdue,
            has_due_date=has_due_date,
            include_completed=include_completed,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            limit=limit,
        )

        items = [await _enrich_task_list_item(task, service.session) for task in tasks]

        total_pages = (total_count + limit - 1) // limit if limit else 0
        payload = TasksPage(
            items=items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Tasks retrieved successfully", data=payload)

    except TaskReadServiceError as exc:
        logger.warning("Tasks access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing tasks: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@tasks_router.get(
    "/tasks/assignees",
    response_model=GenericResponse[TaskAssigneesResponse],
    tags=["crm", "tasks"],
    summary="List task assignees",
    description="Return distinct assignees for task filter controls.",
)
async def list_task_assignees(
    response: FastAPIResponse,
    q: str | None = Query(None, description="Search assignee name or email"),
    limit: int = Query(200, ge=1, le=500),
    service: TaskReadService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TaskAssigneesResponse]:
    try:
        rows = await service.list_task_assignees(q=q, limit=limit)
        options: list[TaskAssigneeOption] = []
        for row in rows:
            assigned_to = _build_task_assignee(row["task"])
            if assigned_to is None:
                continue
            options.append(
                TaskAssigneeOption(
                    assigned_to=assigned_to,
                    task_count=row["task_count"],
                )
            )
        payload = TaskAssigneesResponse(assignees=options)
        return Response.success_response(
            message="Task assignees retrieved successfully",
            data=payload,
        )
    except TaskReadServiceError as exc:
        logger.warning("Task assignee access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing task assignees: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@tasks_router.get(
    "/tasks/assignees/me",
    response_model=GenericResponse[TaskAssigneesResponse],
    tags=["crm", "tasks"],
    summary="Resolve current app user to task assignees",
    description="Return assignee filter options that represent the authenticated user.",
)
async def get_current_user_task_assignees(
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: TaskReadService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TaskAssigneesResponse]:
    try:
        rows = await service.resolve_current_user_task_assignees(
            user_id=user_ids.user_id,
            user_email=user_ids.user_email,
        )
        options = [
            TaskAssigneeOption(
                assigned_to=TaskAssignee(
                    kind=row["kind"],
                    filter_key=row["filter_key"],
                    name=row.get("name"),
                    email=row.get("email"),
                    public_user_id=row.get("public_user_id"),
                    crm_user_id=row.get("crm_user_id"),
                ),
                task_count=row["task_count"],
            )
            for row in rows
        ]
        payload = TaskAssigneesResponse(assignees=options)
        return Response.success_response(
            message="Current user task assignees resolved successfully",
            data=payload,
        )
    except TaskReadServiceError as exc:
        logger.warning(
            "Current user task assignee resolution error: %s", exc.message, exc_info=True
        )
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error(
            "Unexpected error resolving current user task assignees: %s", exc, exc_info=True
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# ── Deal-scoped task list (existing) ─────────────────────────────


@tasks_router.get(
    "/deals/{deal_id}/tasks",
    response_model=GenericResponse[TasksPage],
    tags=["crm", "tasks"],
    summary="List tasks for a specific deal",
    description="Retrieve a paginated list of tasks linked to the specified deal.",
)
async def list_deal_tasks(
    deal_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(50, ge=1, le=100, description="Maximum tasks per page"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    statuses: list[str] | None = Query(None, description="Filter by task statuses"),
    priorities: list[str] | None = Query(None, description="Filter by task priorities"),
    task_types: list[str] | None = Query(
        None,
        description="Filter by task types (TASK/TODO only).",
    ),
    service: TaskReadService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TasksPage]:
    """List tasks for a specific deal."""

    try:
        tasks, total_count = await service.list_tasks_by_deal(
            deal_id=deal_id,
            limit=limit,
            page=page,
            statuses=statuses,
            priorities=priorities,
            task_types=task_types,
        )

        items = [await _enrich_task_list_item(task, service.session) for task in tasks]

        total_pages = (total_count + limit - 1) // limit if limit else 0
        payload = TasksPage(
            items=items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Tasks retrieved successfully", data=payload)

    except TaskReadServiceError as exc:
        logger.warning("Tasks access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing tasks for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@tasks_router.get(
    "/tasks/options",
    response_model=GenericResponse[TaskOptionsResponse],
    tags=["crm", "tasks"],
    summary="Get task option metadata",
    description="Retrieve backend-defined statuses, priorities, task types, and sources for task UI.",
)
async def get_task_options(
    _user_ids: UserIDs = Depends(get_current_user),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TaskOptionsResponse]:
    """Return backend-defined task option metadata for frontend rendering."""

    payload = TaskOptionsResponse(
        statuses=_TASK_STATUS_OPTIONS,
        priorities=_TASK_PRIORITY_OPTIONS,
        task_types=_TASK_TYPE_OPTIONS,
        sources=_TASK_SOURCE_OPTIONS,
    )
    return Response.success_response(
        message="Task options retrieved successfully",
        data=payload,
    )


@tasks_router.get(
    "/tasks/{task_id}",
    response_model=GenericResponse[TaskDetail],
    tags=["crm", "tasks"],
    summary="Get detailed information for a specific task",
    description="Retrieve the full details for a single task by its UUID.",
)
async def get_task(
    task_id: UUID,
    response: FastAPIResponse,
    service: TaskReadService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TaskDetail]:
    """Retrieve detailed information for a single task."""

    try:
        task = await service.get_task_by_id(task_id)

        if not task:
            return Response.error_response(
                message="Task not found", status_code=404, response=response
            )

        detail = await _enrich_task_detail(task, service.session)
        return Response.success_response(message="Task retrieved successfully", data=detail)

    except TaskReadServiceError as exc:
        logger.warning("Task access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error retrieving task %s: %s", task_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@tasks_router.post(
    "/tasks",
    response_model=GenericResponse[TaskDetail],
    status_code=201,
    tags=["crm", "tasks"],
    summary="Create a task",
    description="Create a task. Optionally link to a deal via deal_id in the body.",
)
async def create_task(
    request: CreateTaskRequest,
    response: FastAPIResponse,
    service: TaskWriteService = Depends(get_tasks_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("tasks:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Create a new task, optionally linked to a deal."""

    try:
        payload = request.model_dump(exclude_unset=True)
        deal_id = payload.pop("deal_id", None)
        task = await service.create_task(payload, deal_id=deal_id)
        await service.session.commit()
        await service.session.refresh(task)
        detail = await _enrich_task_detail(task, service.session)
        return Response.created_response(
            message="Task created successfully",
            data=detail,
            response=response,
        )
    except TaskWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Task write error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error creating task: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@tasks_router.post(
    "/deals/{deal_id}/tasks",
    response_model=GenericResponse[TaskDetail],
    status_code=201,
    tags=["crm", "tasks"],
    summary="Create a task for a deal",
    description="Create a user-generated task associated with the specified deal.",
)
async def create_deal_task(
    deal_id: UUID,
    request: CreateTaskRequest,
    response: FastAPIResponse,
    service: TaskWriteService = Depends(get_tasks_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("tasks:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Create a new task associated with a deal (backwards-compatible endpoint)."""

    try:
        payload = request.model_dump(exclude_unset=True)
        payload.pop("deal_id", None)
        task = await service.create_task(payload, deal_id=deal_id)
        await service.session.commit()
        await service.session.refresh(task)
        detail = await _enrich_task_detail(task, service.session)
        return Response.created_response(
            message="Task created successfully",
            data=detail,
            response=response,
        )
    except TaskWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Task write error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error creating task: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@tasks_router.patch(
    "/tasks/{task_id}",
    response_model=GenericResponse[TaskDetail],
    tags=["crm", "tasks"],
    summary="Update a task",
    description="Update a user-generated task by its UUID.",
)
async def update_task(
    task_id: UUID,
    request: UpdateTaskRequest,
    response: FastAPIResponse,
    service: TaskWriteService = Depends(get_tasks_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("tasks:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Update an existing task."""

    try:
        task = await service.update_task(task_id, request.model_dump(exclude_unset=True))
        await service.session.commit()
        await service.session.refresh(task)
        detail = await _enrich_task_detail(task, service.session)
        return Response.success_response(message="Task updated successfully", data=detail)
    except TaskWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Task update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error updating task: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@tasks_router.delete(
    "/tasks/{task_id}",
    response_model=GenericResponse[TaskDetail],
    tags=["crm", "tasks"],
    summary="Delete a task",
    description="Soft delete a user-generated task by its UUID.",
)
async def delete_task(
    task_id: UUID,
    response: FastAPIResponse,
    service: TaskWriteService = Depends(get_tasks_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("tasks:delete")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Soft delete a task."""

    try:
        task = await service.delete_task(task_id)
        await service.session.commit()
        await service.session.refresh(task)
        detail = await _enrich_task_detail(task, service.session)
        return Response.success_response(message="Task deleted successfully", data=detail)
    except TaskWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Task delete error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error deleting task: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
