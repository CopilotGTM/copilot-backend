from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.navigation_preference_models import (
    NavigationPreferences,
    UpdateNavigationPreferencesRequest,
)
from src.api.utils.http_response import Response
from src.service.users.navigation_preferences_service import NavigationPreferencesService

navigation_preferences_router = APIRouter()


@navigation_preferences_router.patch(
    "/navigation",
    response_model=GenericResponse[NavigationPreferences],
)
async def update_navigation_preferences(
    request: UpdateNavigationPreferencesRequest,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[NavigationPreferences]:
    service = NavigationPreferencesService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
    )
    payload = await service.update_preferences(request)
    return Response.success_response(
        message="Navigation preferences updated successfully",
        data=payload,
    )
