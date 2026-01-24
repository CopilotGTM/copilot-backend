"""CRM Deal Tasks API handler."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
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
    TasksPage,
    UpdateTaskRequest,
)
from src.api.utils.http_response import Response
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
        description="Filter by task types (TASK/TODO only). Non-task activity types are not returned.",
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

        items = []
        for task in tasks:
            item = TaskListItem.model_validate(task, from_attributes=True)
            item = item.model_copy(
                update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
            )
            items.append(item)

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

        detail = TaskDetail.model_validate(task, from_attributes=True)
        detail = detail.model_copy(
            update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
        )

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
    "/deals/{deal_id}/tasks",
    response_model=GenericResponse[TaskDetail],
    status_code=201,
    tags=["crm", "tasks"],
    summary="Create a task for a deal",
    description="Create a user-generated task associated with the specified deal.",
)
async def create_task(
    deal_id: UUID,
    request: CreateTaskRequest,
    response: FastAPIResponse,
    service: TasksWriteService = Depends(get_tasks_write_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[TaskDetail]:
    """Create a new task associated with a deal."""

    try:
        task = await service.create_task(deal_id, request.model_dump(exclude_unset=True))
        await service.session.commit()
        detail = TaskDetail.model_validate(task, from_attributes=True)
        detail = detail.model_copy(
            update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
        )
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
        detail = TaskDetail.model_validate(task, from_attributes=True)
        detail = detail.model_copy(
            update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
        )
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
        detail = TaskDetail.model_validate(task, from_attributes=True)
        detail = detail.model_copy(
            update={"is_overdue": _compute_is_overdue(task.due_date, task.status)}
        )
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
