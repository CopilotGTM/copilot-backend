"""CRM Deal Tasks API handler."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_task_models import (
    CreateTaskRequest,
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
from src.service.crm.access.tasks_access_service import (
    TasksAccessService,
    TasksAccessServiceError,
)
from src.service.crm.internal.tasks_write_service import (
    TasksWriteService,
    TasksWriteServiceError,
)

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


async def get_tasks_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a TasksAccessService instance for the current org."""

    service = TasksAccessService(org_session, user_ids.organisation_id)
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

    service = TasksWriteService(org_session, user_ids.organisation_id, user_ids.user_id)
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
    deal_id: UUID | None = Query(None, description="Filter by deal"),
    statuses: list[str] | None = Query(None, description="Filter by statuses"),
    priorities: list[str] | None = Query(None, description="Filter by priorities"),
    source: list[str] | None = Query(None, description="Filter by source"),
    owner_user_id: UUID | None = Query(None, description="Filter by assignee"),
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
    service: TasksAccessService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[TasksPage]:
    """Global task listing with rich filtering."""

    try:
        tasks, total_count = await service.list_tasks(
            deal_id=deal_id,
            statuses=statuses,
            priorities=priorities,
            source=source,
            owner_user_id=owner_user_id,
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

    except TasksAccessServiceError as exc:
        logger.warning("Tasks access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing tasks: %s", exc, exc_info=True)
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
    service: TasksAccessService = Depends(get_tasks_access_service),
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

    except TasksAccessServiceError as exc:
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
    service: TasksAccessService = Depends(get_tasks_access_service),
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

    except TasksAccessServiceError as exc:
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
    service: TasksWriteService = Depends(get_tasks_write_service),
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
    except TasksWriteServiceError as exc:
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
    service: TasksWriteService = Depends(get_tasks_write_service),
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
    except TasksWriteServiceError as exc:
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
    service: TasksWriteService = Depends(get_tasks_write_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Update an existing task."""

    try:
        task = await service.update_task(task_id, request.model_dump(exclude_unset=True))
        await service.session.commit()
        await service.session.refresh(task)
        detail = await _enrich_task_detail(task, service.session)
        return Response.success_response(message="Task updated successfully", data=detail)
    except TasksWriteServiceError as exc:
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
    service: TasksWriteService = Depends(get_tasks_write_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Soft delete a task."""

    try:
        task = await service.delete_task(task_id)
        await service.session.commit()
        await service.session.refresh(task)
        detail = await _enrich_task_detail(task, service.session)
        return Response.success_response(message="Task deleted successfully", data=detail)
    except TasksWriteServiceError as exc:
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
