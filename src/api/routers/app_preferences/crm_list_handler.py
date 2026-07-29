"""Authenticated API route for saving CRM collection sorting."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_list_preference_models import (
    CRMListEntity,
    CRMListSortingPreferences,
    UpdateCRMListSortingRequest,
)
from src.api.utils.http_response import Response
from src.service.users.crm_list_preferences_service import CRMListPreferencesService

crm_list_preferences_router = APIRouter()


@crm_list_preferences_router.patch(
    "/crm/lists/{entity}/sorting",
    response_model=GenericResponse[CRMListSortingPreferences],
)
async def update_crm_list_sorting(
    entity: CRMListEntity,
    request: UpdateCRMListSortingRequest,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CRMListSortingPreferences]:
    """Save one list's sort field and direction for the authenticated user."""
    service = CRMListPreferencesService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
    )
    payload = await service.update_sorting(entity, request)
    return Response.success_response(
        message="CRM list sorting preference updated successfully",
        data=payload,
    )
