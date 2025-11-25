from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_models import (
    BriefingData,
    DealMeetingResponse,
    UpdateMeetingStatusRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm import (
    MeetingsService,
    MeetingsServiceError,
)

meetings_router = APIRouter()
logger = get_logger(__name__)


async def get_meetings_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a MeetingsService instance for the current org."""
    service = MeetingsService(session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Meetings service close failed", exc_info=True)


@meetings_router.get(
    "/{deal_id}/meetings",
    response_model=GenericResponse[list[DealMeetingResponse]],
    summary="Get meetings for a deal",
)
async def get_deal_meetings(
    deal_id: UUID,
    service: MeetingsService = Depends(get_meetings_service),
):
    """
    Get all meetings associated with a specific deal.
    """
    try:
        meetings = await service.list_meetings_by_deal(deal_id)
        return Response.success_response("Meetings retrieved successfully", meetings)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to retrieve meetings for deal %s", deal_id)
        return Response.internal_server_error_response("Failed to retrieve meetings")


@meetings_router.post(
    "/{deal_id}/meetings/{event_id}/briefing",
    response_model=GenericResponse[BriefingData],
    summary="Generate a meeting briefing",
)
async def generate_meeting_briefing(
    deal_id: UUID,
    event_id: UUID,
    service: MeetingsService = Depends(get_meetings_service),
):
    """
    Trigger the generation of a meeting briefing using AI.
    """
    try:
        briefing = await service.generate_briefing(deal_id, event_id)

        if not briefing:
            raise HTTPException(
                status_code=404, detail="Could not generate briefing. Check deal/event existence."
            )

        return Response.success_response("Briefing generated successfully", briefing)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to generate briefing for deal %s event %s", deal_id, event_id)
        return Response.internal_server_error_response("Failed to generate briefing")


@meetings_router.patch(
    "/{deal_id}/meetings/{event_id}/status",
    response_model=GenericResponse[None],
    summary="Update meeting association status",
)
async def update_meeting_status(
    deal_id: UUID,
    event_id: UUID,
    request: UpdateMeetingStatusRequest,
    service: MeetingsService = Depends(get_meetings_service),
):
    """
    Update the status of a meeting association (e.g., CONFIRM or REJECT).
    """
    try:
        await service.update_meeting_status(deal_id, event_id, request.status)
        return Response.success_response(f"Meeting status updated to {request.status.value}")
    except MeetingsServiceError as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail=e.message) from e
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update meeting status for deal %s event %s", deal_id, event_id)
        return Response.internal_server_error_response("Failed to update meeting status")
