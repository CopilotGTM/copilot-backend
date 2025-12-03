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
    DealJobResponse,
    DealMeetingResponse,
    UpdateMeetingStatusRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.infra.utils.logger import get_logger
from src.service.crm import (
    MeetingsService,
    MeetingsServiceError,
)
from src.service.crm.deal_job_sse_service import DealJobSSEService
from src.service.crm.deal_meeting_job_service import (
    DealMeetingJobService,
    DealMeetingJobServiceError,
)
from sse_starlette.sse import EventSourceResponse

meetings_router = APIRouter()
logger = get_logger(__name__)

# Mapping from URL path parameter to DealJobType
MEETING_JOB_TYPE_MAP = {
    "briefing": DealJobType.MEETING_BRIEFING,
    "summary": DealJobType.MEETING_SUMMARY,
    "followup": DealJobType.MEETING_FOLLOWUP,
}


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


async def get_deal_meeting_job_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealMeetingJobService instance for the current org."""
    service = DealMeetingJobService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealMeetingJobService close failed", exc_info=True)


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
    "/{deal_id}/meetings/{event_id}/jobs/{job_type}",
    response_model=GenericResponse[DealJobResponse],
    summary="Initiate a meeting job",
)
async def initiate_meeting_job(
    deal_id: UUID,
    event_id: UUID,
    job_type: str,
    service: DealMeetingJobService = Depends(get_deal_meeting_job_service),
):
    """
    Initiate a meeting job (briefing, summary, or followup).

    - **briefing**: Pre-meeting preparation and context
    - **summary**: Post-meeting summary generation
    - **followup**: Action items and next steps

    If a job is already running, returns the existing job_id (idempotent).
    Use the returned job_id to subscribe to the SSE stream.
    """
    # Validate job type
    if job_type not in MEETING_JOB_TYPE_MAP:
        logger.warning("Invalid meeting job type requested: %s", job_type)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid job type: {job_type}. Must be one of: {list(MEETING_JOB_TYPE_MAP.keys())}",
        )

    deal_job_type = MEETING_JOB_TYPE_MAP[job_type]

    try:
        job = await service.create_meeting_job(deal_id, event_id, deal_job_type)

        # Determine if this is an existing job or a new one
        is_new = job.status == JobStatus.PENDING
        message = (
            f"{job_type.title()} generation initiated"
            if is_new
            else f"{job_type.title()} generation already in progress"
        )

        return Response.accepted_response(
            message,
            DealJobResponse(
                job_id=job.job_id,
                job_type=str(job.job_type),
                status=str(job.status),
            ),
        )
    except DealMeetingJobServiceError as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail=e.message) from e
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to initiate %s for deal %s event %s", job_type, deal_id, event_id)
        return Response.internal_server_error_response(f"Failed to initiate {job_type}")


@meetings_router.get(
    "/{deal_id}/meetings/{event_id}/jobs/{job_id}/stream",
    summary="SSE stream for meeting job",
)
async def meeting_job_stream(
    deal_id: UUID,
    event_id: UUID,
    job_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
):
    """
    SSE stream for any meeting job progress and results.

    Subscribe to this endpoint with the job_id from POST /jobs/{job_type}.
    Handles reconnection - if user disconnects and reconnects, the stream resumes.
    """
    sse_service = DealJobSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    logger.info(
        "Client connected to meeting job SSE: org=%s deal=%s event=%s job=%s",
        user_ids.organisation_id,
        deal_id,
        event_id,
        job_id,
    )

    async def event_generator():
        try:
            async for ev in sse_service.stream_events():
                yield ev
        finally:
            try:
                await sse_service.close()
            except Exception:
                logger.debug("DealJobSSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())


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
