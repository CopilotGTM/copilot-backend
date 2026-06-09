"""Scheduled agents API handler backed by user reminders."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.routers.reminders.reminders_handler import (
    _UNEXPECTED_ERR_MSG,
    get_reminder_service,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.reminder_models import (
    AgentCreateRequest,
    AgentUpdateRequest,
    AvailableChannelsResponse,
    DeliveryChannelInfo,
    ReminderDetail,
    ReminderHistoryQueryParams,
    ReminderHistoryResponse,
    ReminderListQueryParams,
    ReminderListResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.reminder_models import ReminderStatus, ReminderType
from src.infra.utils.logger import get_logger
from src.service.channels import OutboundChannelService
from src.service.reminders.reminder_service import (
    InvalidStatusTransitionError,
    ReminderNotFoundError,
    ReminderService,
    ReminderServiceError,
)

logger = get_logger("agents_handler")

agents_router = APIRouter()


@agents_router.get(
    "/channels",
    response_model=GenericResponse[AvailableChannelsResponse],
    summary="Get available scheduled-agent delivery channels",
    description=(
        "Returns which delivery channels the current user has set up and can receive "
        "scheduled agents on."
    ),
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_available_agent_channels(
    response: FastAPIResponse,
    user: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
) -> GenericResponse[AvailableChannelsResponse]:
    try:
        channel_infos = await OutboundChannelService().list_user_channels(
            master_session,
            user.organisation_id,
            user.user_id,
        )
        channels = [
            DeliveryChannelInfo(
                channel=item.channel,
                available=item.available,
                label=item.label,
                icon_url=item.icon_url,
                mapping_id=item.mapping_id,
            )
            for item in channel_infos
        ]

        return Response.success_response(
            message="Available scheduled-agent channels retrieved successfully",
            data=AvailableChannelsResponse(channels=channels),
        )
    except Exception:
        logger.exception("Failed to get scheduled-agent channels")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@agents_router.post(
    "",
    response_model=GenericResponse[ReminderDetail],
    summary="Create a scheduled agent",
    description="Create a new time-triggered scheduled agent. Goes directly to ACTIVE status.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def create_agent(
    body: AgentCreateRequest,
    response: FastAPIResponse,
    user: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        delivery_target = await OutboundChannelService().resolve_user_channel(
            master_session,
            user.organisation_id,
            user.user_id,
            body.delivery_channel,
        )
        if delivery_target is None:
            return Response.error_response(
                message=(
                    f"Channel '{body.delivery_channel}' is not set up. "
                    "Please connect it first via the bot before creating a scheduled agent."
                ),
                status_code=400,
                response=response,
            )

        item = await service.create(
            title=body.title,
            reminder_type=ReminderType.AGENTIC,
            message_content=None,
            agent_prompt=body.agent_prompt,
            deal_id=body.deal_id,
            trigger_time=body.trigger_time,
            timezone=body.timezone,
            recurrence=body.recurrence,
            recurrence_end_at=body.recurrence_end_at,
            delivery_channel=body.delivery_channel,
            delivery_context=delivery_target.delivery_context,
        )

        return Response.created_response(
            message="Scheduled agent created successfully",
            data=item,
            response=response,
        )

    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to create scheduled agent")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.get(
    "",
    response_model=GenericResponse[ReminderListResponse],
    summary="List scheduled agents",
    description="Get paginated list of scheduled agents for the current user.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def list_agents(
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
    query: ReminderListQueryParams = Depends(),
) -> GenericResponse[ReminderListResponse]:
    try:
        parsed_statuses: list[ReminderStatus] | None = None
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
            reminder_type=ReminderType.AGENTIC,
            sort_order=query.sort,
        )

        total_pages = (total_count + query.limit - 1) // query.limit if query.limit else 0

        return Response.success_response(
            message="Scheduled agents retrieved successfully",
            data=ReminderListResponse(
                items=items,
                page=query.page,
                limit=query.limit,
                total_count=total_count,
                total_pages=total_pages,
            ),
        )

    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to list scheduled agents")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.get(
    "/{reminder_id}/history",
    response_model=GenericResponse[ReminderHistoryResponse],
    summary="Get scheduled agent execution history",
    description="Get paginated execution history for a specific scheduled agent.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_agent_history(
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
            reminder_type=ReminderType.AGENTIC,
        )

        total_pages = (total_count + query.limit - 1) // query.limit if query.limit else 0

        return Response.success_response(
            message="Scheduled agent history retrieved successfully",
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
            message="Scheduled agent not found", status_code=404, response=response
        )
    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to get scheduled agent history")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.get(
    "/{reminder_id}",
    response_model=GenericResponse[ReminderDetail],
    summary="Get scheduled agent details",
    description="Get full details of a specific scheduled agent.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_agent(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.get(reminder_id, reminder_type=ReminderType.AGENTIC)

        return Response.success_response(
            message="Scheduled agent retrieved successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Scheduled agent not found", status_code=404, response=response
        )
    except Exception:
        logger.exception("Failed to get scheduled agent")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.patch(
    "/{reminder_id}",
    response_model=GenericResponse[ReminderDetail],
    summary="Update a scheduled agent",
    description=(
        "Update scheduled agent fields. Schedule is automatically adjusted if time changes."
    ),
    dependencies=[Depends(rate_limit_user_write)],
)
async def update_agent(
    reminder_id: UUID,
    body: AgentUpdateRequest,
    response: FastAPIResponse,
    user: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        updates = body.model_dump(exclude_unset=True)

        if not updates:
            return Response.error_response(
                message="No fields to update", status_code=400, response=response
            )

        if "delivery_channel" in updates:
            delivery_target = await OutboundChannelService().resolve_user_channel(
                master_session,
                user.organisation_id,
                user.user_id,
                updates["delivery_channel"],
            )
            if delivery_target is None:
                return Response.error_response(
                    message=(
                        f"Channel '{updates['delivery_channel']}' is not set up. "
                        "Please connect it first via the bot."
                    ),
                    status_code=400,
                    response=response,
                )
            updates["delivery_context"] = delivery_target.delivery_context

        item = await service.update(reminder_id, updates, reminder_type=ReminderType.AGENTIC)

        return Response.success_response(
            message="Scheduled agent updated successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Scheduled agent not found", status_code=404, response=response
        )
    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to update scheduled agent")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.post(
    "/{reminder_id}/pause",
    response_model=GenericResponse[ReminderDetail],
    summary="Pause a scheduled agent",
    description="Pause an active scheduled agent. Can be resumed later.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def pause_agent(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.pause(reminder_id, reminder_type=ReminderType.AGENTIC)

        return Response.success_response(
            message="Scheduled agent paused successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Scheduled agent not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to pause scheduled agent")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.post(
    "/{reminder_id}/resume",
    response_model=GenericResponse[ReminderDetail],
    summary="Resume a scheduled agent",
    description="Resume a previously paused scheduled agent. Trigger time must be in the future.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def resume_agent(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.resume(reminder_id, reminder_type=ReminderType.AGENTIC)

        return Response.success_response(
            message="Scheduled agent resumed successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Scheduled agent not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to resume scheduled agent")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.post(
    "/{reminder_id}/cancel",
    response_model=GenericResponse[ReminderDetail],
    summary="Cancel a scheduled agent",
    description="Cancel a scheduled agent permanently. This action cannot be undone.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def cancel_agent(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[ReminderDetail]:
    try:
        item = await service.cancel(reminder_id, reminder_type=ReminderType.AGENTIC)

        return Response.success_response(
            message="Scheduled agent cancelled successfully",
            data=item,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Scheduled agent not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to cancel scheduled agent")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@agents_router.delete(
    "/{reminder_id}",
    response_model=GenericResponse[None],
    summary="Delete a scheduled agent",
    description=(
        "Permanently delete a scheduled agent. Consider using cancel instead to preserve history."
    ),
    dependencies=[Depends(rate_limit_user_write)],
)
async def delete_agent(
    reminder_id: UUID,
    response: FastAPIResponse,
    service: ReminderService = Depends(get_reminder_service),
) -> GenericResponse[None]:
    try:
        await service.delete(reminder_id, reminder_type=ReminderType.AGENTIC)

        return Response.success_response(
            message="Scheduled agent deleted successfully",
            data=None,
        )

    except ReminderNotFoundError:
        return Response.error_response(
            message="Scheduled agent not found", status_code=404, response=response
        )
    except ReminderServiceError as e:
        logger.warning("Scheduled agent service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to delete scheduled agent")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
