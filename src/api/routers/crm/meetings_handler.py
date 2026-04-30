from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Response as FastAPIResponse
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
    TranscriptFinalizeRequest,
    TranscriptPresignRequest,
    TranscriptPresignResponse,
    UpdateMeetingStatusRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.infra.utils.logger import get_logger
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService
from src.service.crm.jobs.deal_meeting_job_service import (
    DealMeetingJobService,
    DealMeetingJobServiceError,
)
from src.service.crm.meetings.meetings_service import (
    MeetingsService,
    MeetingsServiceError,
)
from sse_starlette.sse import EventSourceResponse

_ALLOWED_MIMETYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_MAX_TRANSCRIPT_SIZE = 25 * 1024 * 1024

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
    response: FastAPIResponse,
    service: MeetingsService = Depends(get_meetings_service),
) -> GenericResponse[list[DealMeetingResponse]]:
    """
    Get all meetings associated with a specific deal.
    """
    try:
        meetings = await service.list_meetings_by_deal(deal_id)
        return Response.success_response("Meetings retrieved successfully", meetings)
    except MeetingsServiceError as e:
        logger.warning("Meetings service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to retrieve meetings for deal %s", deal_id)
        return Response.error_response(
            message="Failed to retrieve meetings", status_code=500, response=response
        )


@meetings_router.post(
    "/{deal_id}/meetings/{event_id}/jobs/{job_type}",
    response_model=GenericResponse[DealJobResponse],
    summary="Initiate a meeting job",
)
async def initiate_meeting_job(
    deal_id: UUID,
    event_id: UUID,
    job_type: str,
    response: FastAPIResponse,
    service: DealMeetingJobService = Depends(get_deal_meeting_job_service),
) -> GenericResponse[DealJobResponse]:
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
        return Response.error_response(
            message=f"Invalid job type: {job_type}. Must be one of: {list(MEETING_JOB_TYPE_MAP.keys())}",
            status_code=400,
            response=response,
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
            response=response,
        )
    except DealMeetingJobServiceError as e:
        logger.warning("Meeting job service error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to initiate %s for deal %s event %s", job_type, deal_id, event_id)
        return Response.error_response(
            message=f"Failed to initiate {job_type}", status_code=500, response=response
        )


@meetings_router.get(
    "/{deal_id}/meetings/{event_id}/jobs/{job_id}/stream",
    summary="SSE stream for meeting job",
)
async def meeting_job_stream(
    deal_id: UUID,
    event_id: UUID,
    job_id: UUID,
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
):
    """
    SSE stream for any meeting job progress and results.

    Subscribe to this endpoint with the job_id from POST /jobs/{job_type}.
    Handles reconnection - if user disconnects and reconnects, the stream resumes.
    """
    sse_service = DealJobSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    can_access = await sse_service.ensure_user_access(deal_id=deal_id)
    if can_access is None:
        raise HTTPException(status_code=404, detail="Job stream not found")
    if can_access is False:
        raise HTTPException(status_code=403, detail="Not authorized to access this job stream")

    last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
    resume_from_id = last_event_id or None

    logger.info(
        "Client connected to meeting job SSE: org=%s deal=%s event=%s job=%s",
        user_ids.organisation_id,
        deal_id,
        event_id,
        job_id,
    )

    async def event_generator():
        try:
            async for ev in sse_service.stream_events(resume_from_id=resume_from_id):
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
    response: FastAPIResponse,
    service: MeetingsService = Depends(get_meetings_service),
) -> GenericResponse[None]:
    """
    Update the status of a meeting association (e.g., CONFIRM or REJECT).
    """
    try:
        try:
            parsed_status = AssociationStatus(request.status)
        except ValueError:
            return Response.error_response(
                message=f"Invalid status: {request.status}",
                status_code=400,
                response=response,
            )

        await service.update_meeting_status(deal_id, event_id, parsed_status)
        return Response.success_response(f"Meeting status updated to {parsed_status.value}")
    except MeetingsServiceError as e:
        logger.warning("Meeting status update error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to update meeting status for deal %s event %s", deal_id, event_id)
        return Response.error_response(
            message="Failed to update meeting status", status_code=500, response=response
        )


@meetings_router.post(
    "/{deal_id}/meetings/{event_id}/transcript/presign",
    response_model=GenericResponse[TranscriptPresignResponse],
    status_code=201,
    summary="Presign transcript upload",
)
async def presign_transcript_upload(
    deal_id: UUID,
    event_id: UUID,
    request: TranscriptPresignRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: MeetingsService = Depends(get_meetings_service),
) -> GenericResponse[TranscriptPresignResponse]:
    """Generate a presigned SAS URL for direct transcript file upload.

    Validates the meeting association, checks for duplicates, creates a
    PENDING job, and returns a time-limited write URL. The client uploads
    the file directly to Azure Blob Storage using this URL, then calls
    the finalize endpoint.
    """
    if request.mimetype not in _ALLOWED_MIMETYPES:
        return Response.error_response(
            message="Invalid file type. Only PDF and Word (.docx) files are accepted.",
            status_code=400,
            response=response,
        )
    if request.size_bytes > _MAX_TRANSCRIPT_SIZE:
        return Response.error_response(
            message="File too large. Maximum size is 25 MB.",
            status_code=400,
            response=response,
        )

    try:
        job, upload_url, expires_in = await service.presign_transcript_upload(
            deal_id=deal_id,
            event_id=event_id,
            user_id=user_ids.user_id,
            filename=request.filename,
            mimetype=request.mimetype,
            size_bytes=request.size_bytes,
        )

        # Idempotent — existing job has no new URL
        if not upload_url:
            return Response.accepted_response(
                "Transcript upload already in progress",
                TranscriptPresignResponse(
                    job_id=job.job_id,
                    upload_url="",
                    expires_in_seconds=0,
                    status=str(job.status),
                ),
                response=response,
            )

        return Response.created_response(
            "Transcript upload presigned",
            TranscriptPresignResponse(
                job_id=job.job_id,
                upload_url=upload_url,
                expires_in_seconds=expires_in,
                status=str(job.status),
            ),
            response=response,
        )
    except MeetingsServiceError as e:
        logger.warning("Transcript presign error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to presign transcript for deal %s event %s", deal_id, event_id)
        return Response.error_response(
            message="Failed to presign transcript upload", status_code=500, response=response
        )


@meetings_router.post(
    "/{deal_id}/meetings/{event_id}/transcript/finalize",
    response_model=GenericResponse[DealJobResponse],
    summary="Finalize transcript upload",
)
async def finalize_transcript_upload(
    deal_id: UUID,
    event_id: UUID,
    request: TranscriptFinalizeRequest,
    response: FastAPIResponse,
    service: MeetingsService = Depends(get_meetings_service),
) -> GenericResponse[DealJobResponse]:
    """Confirm that the transcript file has been uploaded to blob storage.

    Marks the job as IN_PROGRESS and enqueues the extraction worker which
    parses the file, creates a Meeting record, and triggers summary generation.
    Subscribe to the SSE stream using the returned job_id.
    """
    try:
        job = await service.finalize_transcript_upload(
            deal_id=deal_id,
            event_id=event_id,
            job_id=request.job_id,
        )

        return Response.accepted_response(
            "Transcript upload finalized — extraction started",
            DealJobResponse(
                job_id=job.job_id,
                job_type=str(job.job_type),
                status=str(job.status),
            ),
            response=response,
        )
    except MeetingsServiceError as e:
        logger.warning("Transcript finalize error: %s", e.message)
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to finalize transcript for deal %s event %s", deal_id, event_id)
        return Response.error_response(
            message="Failed to finalize transcript upload", status_code=500, response=response
        )
