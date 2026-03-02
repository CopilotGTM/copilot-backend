"""Deal Memory API handler — endpoint for retrieving deal memory documents."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.deal_memory_models import DealMemoryResponse
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.deal_memory_models import DealMemory
from src.infra.utils.logger import get_logger

logger = get_logger("deal_memory_handler")

deal_memory_router = APIRouter()


@deal_memory_router.get(
    "/{deal_id}/memory",
    response_model=GenericResponse[DealMemoryResponse | None],
    summary="Get deal memory",
    description="Retrieve the persistent memory document for a deal",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_deal_memory(
    deal_id: UUID,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
) -> GenericResponse[DealMemoryResponse | None]:
    """Get the memory document for a deal.

    Returns the markdown memory content if it exists, or null data with a
    success message if no memory has been built yet.
    """
    try:
        result = await session.execute(
            select(DealMemory).where(
                col(DealMemory.deal_id) == deal_id,
                col(DealMemory.organisation_id) == user_ids.organisation_id,
            )
        )
        memory = result.scalars().first()

        if not memory:
            return Response.success_response(
                message="No memory exists for this deal yet",
                data=None,
            )

        return Response.success_response(
            message="Deal memory retrieved successfully",
            data=DealMemoryResponse.model_validate(memory),
        )

    except Exception as exc:
        logger.error("Error retrieving memory for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message="An unexpected error occurred while retrieving deal memory",
            status_code=500,
            response=response,
        )
