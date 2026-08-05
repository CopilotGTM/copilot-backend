"""Authenticated API route for saving CRM collection sorting."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_list_preference_models import (
    CRMColumnEntity,
    CRMListConfiguration,
    CRMListEntity,
    CRMListSortingPreferences,
    UpdateCRMListColumnsRequest,
    UpdateCRMListSortingRequest,
)
from src.api.utils.http_response import Response
from src.service.users.crm_list_preferences_service import CRMListPreferencesService

crm_list_preferences_router = APIRouter()


@crm_list_preferences_router.get(
    "/crm/lists/{entity}",
    response_model=GenericResponse[CRMListConfiguration],
)
async def get_crm_list_configuration(
    entity: CRMColumnEntity,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    org_session: AsyncSession = Depends(get_org_async_session),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMListConfiguration]:
    """Return the backend-owned catalog and current ordered selection."""
    service = CRMListPreferencesService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
        org_session=org_session,
    )
    payload = await service.get_configuration(entity)
    return Response.success_response(
        message="CRM list configuration retrieved successfully",
        data=payload,
    )


@crm_list_preferences_router.put(
    "/crm/lists/{entity}",
    response_model=GenericResponse[CRMListConfiguration],
)
async def update_crm_list_columns(
    entity: CRMColumnEntity,
    request: UpdateCRMListColumnsRequest,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    org_session: AsyncSession = Depends(get_org_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CRMListConfiguration]:
    """Save the authenticated user's complete ordered visible selection."""
    service = CRMListPreferencesService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
        org_session=org_session,
    )
    payload = await service.update_columns(entity, request)
    return Response.success_response(
        message="CRM list columns updated successfully",
        data=payload,
    )


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
