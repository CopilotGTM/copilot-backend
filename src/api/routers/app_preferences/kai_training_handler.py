"""User-facing Kai Training profile routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.kai_training_models import KaiTrainingResponse, UpdateKaiTrainingRequest
from src.api.utils.http_response import Response
from src.domain.models.master_tables import UserAppRole
from src.service.orgnisation.kai_training_service import KaiTrainingService
from src.workers.organization_worker.client import send_organization_research_task

kai_training_router = APIRouter()


def _is_admin(user_ids: UserIDs) -> bool:
    return bool(user_ids.user_role == UserAppRole.ADMIN)


def _require_admin(user_ids: UserIDs) -> None:
    if not _is_admin(user_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation admin access is required",
        )


@kai_training_router.get(
    "/kai-training",
    response_model=GenericResponse[KaiTrainingResponse],
)
async def get_kai_training(
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[KaiTrainingResponse]:
    payload = await KaiTrainingService(
        master_session, user_ids.organisation_id
    ).get_response(can_edit=_is_admin(user_ids))
    return Response.success_response(
        message="Kai Training profile retrieved successfully",
        data=payload,
    )


@kai_training_router.patch(
    "/kai-training",
    response_model=GenericResponse[KaiTrainingResponse],
)
async def update_kai_training(
    request: UpdateKaiTrainingRequest,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[KaiTrainingResponse]:
    _require_admin(user_ids)
    payload = await KaiTrainingService(
        master_session, user_ids.organisation_id
    ).update_user_values(request, can_edit=True)
    return Response.success_response(
        message="Kai Training profile saved successfully",
        data=payload,
    )


@kai_training_router.post(
    "/kai-training/research",
    response_model=GenericResponse[KaiTrainingResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def research_kai_training(
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[KaiTrainingResponse]:
    _require_admin(user_ids)
    service = KaiTrainingService(master_session, user_ids.organisation_id)
    payload, should_enqueue = await service.mark_pending()
    if should_enqueue:
        try:
            await send_organization_research_task(str(user_ids.organisation_id))
        except Exception:
            await service.mark_failed("Research could not be queued. Please try again.")
            raise
    return Response.accepted_response(
        message="Kai Training research queued",
        data=payload,
    )
