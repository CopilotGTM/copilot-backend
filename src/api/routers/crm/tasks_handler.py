"""CRM Deal Tasks API handler."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_task_models import (
    TaskDetail,
    TaskListItem,
    TasksPage,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.tasks_access_service import (
    TasksAccessService,
    TasksAccessServiceError,
)

logger = get_logger("tasks_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

tasks_router = APIRouter()


def _compute_is_overdue(due_date: datetime | None, status: str | None) -> bool:
    """Return True when a task is past due and not completed."""

    if not due_date or status == "COMPLETED":
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


@tasks_router.get(
    "/deals/{deal_id}/tasks",
    response_model=GenericResponse[TasksPage],
    tags=["crm", "tasks"],
    summary="List tasks for a specific deal",
    description="Retrieve a paginated list of tasks linked to the specified deal.",
)
async def list_deal_tasks(
    deal_id: UUID,
    limit: int = Query(50, ge=1, le=100, description="Maximum tasks per page"),
    cursor: str | None = Query(None, description="Pagination cursor from a previous response"),
    statuses: list[str] | None = Query(None, description="Filter by task statuses"),
    priorities: list[str] | None = Query(None, description="Filter by task priorities"),
    task_types: list[str] | None = Query(
        None,
        description="Filter by task types (TASK/TODO only). Non-task activity types are not returned.",
    ),
    service: TasksAccessService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    """List tasks for a specific deal."""

    try:
        tasks, next_cursor = await service.list_tasks_by_deal(
            deal_id=deal_id,
            limit=limit,
            cursor=cursor,
            statuses=statuses,
            priorities=priorities,
            task_types=task_types,
        )

        items = []
        for task in tasks:
            item = TaskListItem.model_validate(task, from_attributes=True)
            item = item.model_copy(
                update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
            )
            items.append(item)

        page = TasksPage(items=items, cursor=next_cursor)
        return Response.success_response(message="Tasks retrieved successfully", data=page)

    except TasksAccessServiceError as exc:
        logger.warning("Tasks access error: %s", exc.message, exc_info=True)
        return Response.error_response(message=exc.message, status_code=exc.status_code)
    except Exception as exc:
        logger.error("Unexpected error listing tasks for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(message=_UNEXPECTED_ERR_MSG, status_code=500)


@tasks_router.get(
    "/tasks/{task_id}",
    response_model=GenericResponse[TaskDetail],
    tags=["crm", "tasks"],
    summary="Get detailed information for a specific task",
    description="Retrieve the full details for a single task by its UUID.",
)
async def get_task(
    task_id: UUID,
    service: TasksAccessService = Depends(get_tasks_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    """Retrieve detailed information for a single task."""

    try:
        task = await service.get_task_by_id(task_id)

        if not task:
            return Response.error_response(message="Task not found", status_code=404)

        detail = TaskDetail.model_validate(task, from_attributes=True)
        detail = detail.model_copy(
            update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
        )

        return Response.success_response(message="Task retrieved successfully", data=detail)

    except TasksAccessServiceError as exc:
        logger.warning("Task access error: %s", exc.message, exc_info=True)
        return Response.error_response(message=exc.message, status_code=exc.status_code)
    except Exception as exc:
        logger.error("Unexpected error retrieving task %s: %s", task_id, exc, exc_info=True)
        return Response.error_response(message=_UNEXPECTED_ERR_MSG, status_code=500)
