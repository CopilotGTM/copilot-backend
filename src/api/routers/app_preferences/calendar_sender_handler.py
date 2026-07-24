"""Authenticated endpoint for a user's preferred outbound calendar."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.calendar_sender_preference_models import (
    CalendarSenderPreference,
    UpdateCalendarSenderPreferenceRequest,
)
from src.api.utils.http_response import Response
from src.service.users.calendar_sender_preferences_service import (
    CalendarSenderPreferencesService,
)

calendar_sender_preferences_router = APIRouter()


@calendar_sender_preferences_router.patch(
    "/calendar-sender",
    response_model=GenericResponse[CalendarSenderPreference],
)
async def update_calendar_sender_preference(
    request: UpdateCalendarSenderPreferenceRequest,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    org_session: AsyncSession = Depends(get_org_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CalendarSenderPreference]:
    """Set or clear the authenticated user's preferred connected calendar."""
    service = CalendarSenderPreferencesService(
        master_session=master_session,
        org_session=org_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
    )
    payload = await service.update(
        str(request.preferred_sender_email) if request.preferred_sender_email else None
    )
    return Response.success_response(
        message="Calendar sender preference updated successfully",
        data=payload,
    )
