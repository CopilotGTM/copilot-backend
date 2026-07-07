from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.meeting_recording_policy_models import (
    MeetingRecordingPolicyResponse,
    UpdateMeetingRecordingPolicyRequest,
)
from src.api.utils.http_response import Response
from src.service.orgnisation.meeting_recording_policy_settings_service import (
    MeetingRecordingPolicySettingsService,
)

meeting_recording_policy_router = APIRouter()


@meeting_recording_policy_router.get(
    "/meeting-recording",
    response_model=GenericResponse[MeetingRecordingPolicyResponse],
)
async def get_meeting_recording_policy(
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[MeetingRecordingPolicyResponse]:
    service = MeetingRecordingPolicySettingsService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
    )
    payload = await service.get_policy()
    return Response.success_response(
        message="Meeting recording policy retrieved successfully",
        data=payload,
    )


@meeting_recording_policy_router.patch(
    "/meeting-recording",
    response_model=GenericResponse[MeetingRecordingPolicyResponse],
)
async def update_meeting_recording_policy(
    request: UpdateMeetingRecordingPolicyRequest,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[MeetingRecordingPolicyResponse]:
    service = MeetingRecordingPolicySettingsService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
    )
    payload = await service.update_policy(request)
    return Response.success_response(
        message="Meeting recording policy updated successfully",
        data=payload,
    )
