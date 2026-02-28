"""Reminders API handler for managing user reminders and scheduled alerts."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.reminder_models import (
    ReminderCreateRequest,
    ReminderDetail,
    ReminderHistoryQueryParams,
    ReminderHistoryResponse,
    ReminderListQueryParams,
    ReminderListResponse,
    ReminderUpdateRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.reminder_models import ReminderStatus
from src.infra.utils.logger import get_logger
from src.service.reminders.reminder_service import (
    InvalidStatusTransitionError,
    ReminderNotFoundError,
    ReminderService,
    ReminderServiceError,
)

logger = get_logger("reminders_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

reminders_router = APIRouter()


async def get_reminder_service(
    user: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a ReminderService for the current user/org."""
    service = ReminderService(
        session=session,
        organisation_id=user.organisation_id,
        user_id=user.user_id,
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("ReminderService close failed", exc_info=True)


@reminders_router.post(
    "",
    response_model=GenericResponse[ReminderDetail],
    summary="Create a reminder",
    description="Create a new reminder or scheduled alert. Goes directly to ACTIVE status.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def create_reminder(
    body: ReminderCreateRequest,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.create(
            title=body.title,
            reminder_type=body.reminder_type,
            message_content=body.message_content,
            agent_prompt=body.agent_prompt,
            deal_id=body.deal_id,
            trigger_time=body.trigger_time,
            timezone=body.timezone,
            recurrence=body.recurrence,
            recurrence_end_at=body.recurrence_end_at,
            delivery_channel=body.delivery_channel,
        )

        return Response.created_response(
            message="Reminder created successfully",
            data=item,
            response=response,
        )

    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to create reminder")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.get(
    "",
    response_model=GenericResponse[ReminderListResponse],
    summary="List reminders",
    description="Get paginated list of reminders for the current user.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def list_reminders(
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
    query: ReminderListQueryParams = Depends(),
) -> GenericResponse[ReminderListResponse]:
    try:
        parsed_statuses = None
        if query.status:
            status_strs = [s.strip().lower() for s in query.status.split(",")]
            parsed_statuses = []
            for s in status_strs:
                try:
                    parsed_statuses.append(ReminderStatus(s))
                except ValueError:
                    logger.warning("Invalid status filter: %s", s)
            if not parsed_statuses:
                parsed_statuses = None

        items, total_count = await service.list_reminders(
            limit=query.limit,
            page=query.page,
            statuses=parsed_statuses,
            reminder_type=query.reminder_type,
            sort_order=query.sort,
        )

        total_pages = (total_count + query.limit - 1) // query.limit if query.limit else 0

        return Response.success_response(
            message="Reminders retrieved successfully",
            data=ReminderListResponse(
                items=items,
                page=query.page,
                limit=query.limit,
                total_count=total_count,
                total_pages=total_pages,
            ),
        )

    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to list reminders")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.get(
    "/{reminder_id}/history",
    response_model=GenericResponse[ReminderHistoryResponse],
    summary="Get reminder execution history",
    description="Get paginated execution history for a specific reminder.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_reminder_history(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
    query: ReminderHistoryQueryParams = Depends(),
) -> GenericResponse[ReminderHistoryResponse]:
    try:
        items, total_count = await service.list_history(
            reminder_id,
            limit=query.limit,
            page=query.page,
        )

        total_pages = (total_count + query.limit - 1) // query.limit if query.limit else 0

        return Response.success_response(
            message="Reminder history retrieved successfully",
            data=ReminderHistoryResponse(
                items=items,
                page=query.page,
                limit=query.limit,
                total_count=total_count,
                total_pages=total_pages,
            ),
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Reminder not found", status_code=404, response=response
        )
    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to get reminder history")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.get(
    "/{reminder_id}",
    response_model=GenericResponse[ReminderDetail],
    summary="Get reminder details",
    description="Get full details of a specific reminder.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_reminder(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.get(reminder_id)

        return Response.success_response(
            message="Reminder retrieved successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Reminder not found", status_code=404, response=response
        )
    except Exception:
        logger.exception("Failed to get reminder")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.patch(
    "/{reminder_id}",
    response_model=GenericResponse[ReminderDetail],
    summary="Update a reminder",
    description="Update reminder fields. Schedule is automatically adjusted if time changes.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def update_reminder(
    reminder_id: UUID,
    body: ReminderUpdateRequest,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        updates = body.model_dump(exclude_unset=True)

        if not updates:
            return Response.error_response(
                message="No fields to update", status_code=400, response=response
            )

        item = await service.update(reminder_id, updates)

        return Response.success_response(
            message="Reminder updated successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Reminder not found", status_code=404, response=response
        )
    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to update reminder")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.post(
    "/{reminder_id}/pause",
    response_model=GenericResponse[ReminderDetail],
    summary="Pause a reminder",
    description="Pause an active reminder. Can be resumed later.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def pause_reminder(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.pause(reminder_id)

        return Response.success_response(
            message="Reminder paused successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Reminder not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to pause reminder")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.post(
    "/{reminder_id}/resume",
    response_model=GenericResponse[ReminderDetail],
    summary="Resume a paused reminder",
    description="Resume a previously paused reminder. Trigger time must be in the future.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def resume_reminder(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.resume(reminder_id)

        return Response.success_response(
            message="Reminder resumed successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Reminder not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to resume reminder")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.post(
    "/{reminder_id}/cancel",
    response_model=GenericResponse[ReminderDetail],
    summary="Cancel a reminder",
    description="Cancel a reminder permanently. This action cannot be undone.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def cancel_reminder(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.cancel(reminder_id)

        return Response.success_response(
            message="Reminder cancelled successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Reminder not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to cancel reminder")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@reminders_router.delete(
    "/{reminder_id}",
    response_model=GenericResponse[None],
    summary="Delete a reminder",
    description="Permanently delete a reminder. Consider using cancel instead to preserve history.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def delete_reminder(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[None]:
    try:
        await service.delete(reminder_id)

        return Response.success_response(
            message="Reminder deleted successfully",
            data=None,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Reminder not found", status_code=404, response=response
        )
    except ReminderServiceError as e:
        logger.warning("Reminder service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to delete reminder")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
