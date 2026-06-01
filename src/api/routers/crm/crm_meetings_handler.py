"""Top-level CRM meetings API for authenticated users."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_meeting_models import CRMMeetingsPage
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.meetings_access_service import (
    CRMMeetingRecordingControlService,
    CRMMeetingsAccessService,
    CRMMeetingsAccessServiceError,
)

crm_meetings_router = APIRouter()
logger = get_logger(__name__)


async def get_crm_meetings_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Create the current-user CRM meetings access service."""
    service = CRMMeetingsAccessService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        await service.close()


def get_crm_meeting_recording_control_service(
    user_ids: UserIDs = Depends(get_current_user),
) -> CRMMeetingRecordingControlService:
    """Create provider-facing recording control service after ownership checks."""
    return CRMMeetingRecordingControlService(user_ids.organisation_id)


@crm_meetings_router.get(
    "/meetings",
    response_model=GenericResponse[CRMMeetingsPage],
    summary="List current user's CRM meetings",
)
async def list_crm_meetings(
    response: FastAPIResponse,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    start_from: datetime | None = Query(None),
    start_to: datetime | None = Query(None),
    time_scope: str | None = Query(None, description="upcoming, past, or all"),
    recording_status: str | None = Query(None),
    linked_deal: bool | None = Query(None),
    service: CRMMeetingsAccessService = Depends(get_crm_meetings_access_service),
) -> GenericResponse[CRMMeetingsPage]:
    """List synced meetings visible through the authenticated user's calendar integration."""
    try:
        page_data = await service.list_meetings(
            page=page,
            limit=limit,
            start_from=start_from,
            start_to=start_to,
            time_scope=time_scope,
            recording_status=recording_status,
            linked_deal=linked_deal,
        )
        return Response.success_response("Meetings retrieved successfully", page_data)
    except CRMMeetingsAccessServiceError as exc:
        return Response.error_response(exc.status_code, exc.message, response=response)
    except Exception:
        logger.exception("Failed to list CRM meetings")
        return Response.error_response(500, "Failed to retrieve meetings", response=response)


@crm_meetings_router.post(
    "/meetings/{event_id}/recording-bot",
    response_model=GenericResponse[dict[str, str]],
    summary="Invite recording bot to a user-owned meeting",
)
async def invite_recording_bot(
    event_id: UUID,
    response: FastAPIResponse,
    access_service: CRMMeetingsAccessService = Depends(get_crm_meetings_access_service),
    recording_service: CRMMeetingRecordingControlService = Depends(
        get_crm_meeting_recording_control_service
    ),
) -> GenericResponse[dict[str, str]]:
    """Invite the recording bot after proving the event belongs to the current user."""
    try:
        resolved_event_id = await access_service.resolve_visible_event_id(event_id)
        await access_service.close()
        result = await recording_service.invite_bot(resolved_event_id)
        return Response.success_response("Recording bot invite processed", result)
    except CRMMeetingsAccessServiceError as exc:
        return Response.error_response(exc.status_code, exc.message, response=response)
    except Exception:
        logger.exception("Failed to invite recording bot for event %s", event_id)
        return Response.error_response(500, "Failed to invite recording bot", response=response)


@crm_meetings_router.delete(
    "/meetings/{event_id}/recording-bot",
    response_model=GenericResponse[dict[str, str]],
    summary="Remove scheduled recording bot from a user-owned meeting",
)
async def remove_recording_bot(
    event_id: UUID,
    response: FastAPIResponse,
    access_service: CRMMeetingsAccessService = Depends(get_crm_meetings_access_service),
    recording_service: CRMMeetingRecordingControlService = Depends(
        get_crm_meeting_recording_control_service
    ),
) -> GenericResponse[dict[str, str]]:
    """Cancel a scheduled bot; v1 rejects in-call or completed recording sessions."""
    try:
        resolved_event_id = await access_service.resolve_visible_event_id(event_id)
        await access_service.close()
        result = await recording_service.cancel_bot(resolved_event_id)
        return Response.success_response("Recording bot cancelled", result)
    except CRMMeetingsAccessServiceError as exc:
        return Response.error_response(exc.status_code, exc.message, response=response)
    except Exception:
        logger.exception("Failed to remove recording bot for event %s", event_id)
        return Response.error_response(500, "Failed to remove recording bot", response=response)
