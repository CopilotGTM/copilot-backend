"""Top-level Meetings tab API routes.

These routes are user-calendar scoped, not deal scoped. Existing deal meeting
routes remain mounted under `/users/crm/deals` for prep, summary, follow-up,
and manual transcript upload workflows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.meetings_tab_models import (
    CreateMeetingRequest,
    CreateMeetingResponse,
    LinkMeetingRequest,
    MeetingDetail,
    MeetingLinkedRecordType,
    MeetingRecordingResponse,
    MeetingsPage,
    MeetingsView,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.calendar_invites.models import CalendarInviteError
from src.service.meetings_tab_service import MeetingsTabService, MeetingsTabServiceError

logger = get_logger(__name__)
meetings_tab_router = APIRouter()


async def _safe_auto_invite(
    service: MeetingsTabService,
    event_id: UUID,
) -> dict[str, Any] | None:
    try:
        return await service.auto_invite_if_eligible(event_id)
    except Exception:
        logger.exception("Auto invite failed after meeting write")
        return {
            "status": "failed",
            "reason": "auto_invite_failed",
            "message": "Auto invite failed after the meeting was saved.",
        }


async def get_meetings_tab_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Yield a MeetingsTabService bound to the authenticated user and org."""
    service = MeetingsTabService(
        org_session,
        master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
        user_timezone=user_ids.user_timezone,
    )
    try:
        yield service
    finally:
        await service.close()


@meetings_tab_router.get(
    "",
    response_model=GenericResponse[MeetingsPage],
    summary="List current-user meetings",
)
async def list_meetings(
    response: FastAPIResponse,
    view: MeetingsView = Query(MeetingsView.TODAY),
    start_at: datetime | None = Query(None),
    end_at: datetime | None = Query(None),
    q: str | None = Query(None, description="Search title, organiser, or attendee"),
    linked_type: MeetingLinkedRecordType | None = Query(None),
    bot_status: str | None = Query(None),
    recording_status: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    service: MeetingsTabService = Depends(get_meetings_tab_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[MeetingsPage]:
    """List meetings from the authenticated user's connected calendars."""
    try:
        data = await service.list_meetings(
            view=view,
            start_at=start_at,
            end_at=end_at,
            q=q,
            linked_type=linked_type,
            bot_status=bot_status,
            recording_status=recording_status,
            page=page,
            limit=limit,
        )
        return Response.success_response("Meetings retrieved", data)
    except MeetingsTabServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Failed to list meetings")
        return Response.error_response(
            status_code=500,
            message="Failed to list meetings",
            response=response,
        )


@meetings_tab_router.get(
    "/{event_id}",
    response_model=GenericResponse[MeetingDetail],
    summary="Get meeting drawer detail",
)
async def get_meeting_detail(
    event_id: UUID,
    response: FastAPIResponse,
    service: MeetingsTabService = Depends(get_meetings_tab_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[MeetingDetail]:
    """Return drawer detail after proving current-user calendar ownership."""
    try:
        data = await service.get_meeting_detail(event_id)
        return Response.success_response("Meeting retrieved", data)
    except MeetingsTabServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Failed to fetch meeting detail")
        return Response.error_response(
            status_code=500,
            message="Failed to fetch meeting",
            response=response,
        )


@meetings_tab_router.post(
    "",
    response_model=GenericResponse[CreateMeetingResponse],
    status_code=201,
    summary="Create a provider calendar meeting",
)
async def create_meeting(
    request: CreateMeetingRequest,
    response: FastAPIResponse,
    service: MeetingsTabService = Depends(get_meetings_tab_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("meetings:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CreateMeetingResponse]:
    """Create a real calendar invite and optionally link it to a deal."""
    try:
        data = await service.create_meeting(request)
        await service.session.commit()
        data.bot_dispatch = await _safe_auto_invite(service, data.event_id)
        return Response.created_response("Meeting created", data, response=response)
    except CalendarInviteError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except MeetingsTabServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Failed to create meeting")
        return Response.error_response(
            status_code=500,
            message="Failed to create meeting",
            response=response,
        )


@meetings_tab_router.patch(
    "/{event_id}/link",
    response_model=GenericResponse[MeetingDetail],
    summary="Link a meeting to a deal",
)
async def link_meeting(
    event_id: UUID,
    request: LinkMeetingRequest,
    response: FastAPIResponse,
    service: MeetingsTabService = Depends(get_meetings_tab_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("meetings:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[MeetingDetail]:
    """Set the active deal link for one current-user meeting."""
    try:
        data = await service.set_link(event_id, request.record_type, request.record_id)
        await service.session.commit()
        await _safe_auto_invite(service, event_id)
        return Response.success_response("Meeting linked", data)
    except MeetingsTabServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Failed to link meeting")
        return Response.error_response(
            status_code=500,
            message="Failed to link meeting",
            response=response,
        )


@meetings_tab_router.post(
    "/{event_id}/bot/invite",
    response_model=GenericResponse[dict],
    summary="Invite the meeting bot",
)
async def invite_bot(
    event_id: UUID,
    response: FastAPIResponse,
    service: MeetingsTabService = Depends(get_meetings_tab_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("meetings:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    """Manually invite Recall bot to a current-user calendar event."""
    try:
        data = await service.invite_bot(event_id)
        if data.get("reason") == "meeting_already_started":
            return Response.error_response(
                status_code=409,
                message="Meeting already started",
                response=response,
            )
        return Response.success_response("Bot invite processed", data)
    except MeetingsTabServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Failed to invite meeting bot")
        return Response.error_response(
            status_code=500,
            message="Failed to invite bot",
            response=response,
        )


@meetings_tab_router.delete(
    "/{event_id}/bot/invite",
    response_model=GenericResponse[dict],
    summary="Remove the meeting bot",
)
async def remove_bot(
    event_id: UUID,
    response: FastAPIResponse,
    service: MeetingsTabService = Depends(get_meetings_tab_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("meetings:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    """Cancel a scheduled Recall bot for a current-user calendar event."""
    try:
        data = await service.cancel_bot(event_id)
        return Response.success_response("Bot invite removed", data)
    except MeetingsTabServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Failed to remove meeting bot")
        return Response.error_response(
            status_code=500,
            message="Failed to remove bot",
            response=response,
        )


@meetings_tab_router.get(
    "/{event_id}/recording",
    response_model=GenericResponse[MeetingRecordingResponse],
    summary="Get Recall recording playback and download detail",
)
async def get_recording(
    event_id: UUID,
    response: FastAPIResponse,
    service: MeetingsTabService = Depends(get_meetings_tab_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[MeetingRecordingResponse]:
    """Return Recall playback/download URLs and transcript metadata for one meeting."""
    try:
        data = await service.get_recording(event_id)
        return Response.success_response("Recording retrieved", data)
    except MeetingsTabServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Failed to fetch meeting recording")
        return Response.error_response(
            status_code=500,
            message="Failed to fetch recording",
            response=response,
        )
