from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.onboarding_preference_models import (
    OnboardingPreferences,
    OnboardingStepId,
)
from src.api.utils.http_response import Response
from src.service.users.onboarding_preferences_service import OnboardingPreferencesService

onboarding_preferences_router = APIRouter()


@onboarding_preferences_router.patch(
    "/onboarding/{step_id}/skip",
    response_model=GenericResponse[OnboardingPreferences],
)
async def skip_onboarding_step(
    step_id: OnboardingStepId,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[OnboardingPreferences]:
    service = OnboardingPreferencesService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
    )
    payload = await service.skip_step(step_id)
    return Response.success_response(
        message="Onboarding step skipped successfully",
        data=payload,
    )
