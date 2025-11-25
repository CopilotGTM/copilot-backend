"""CRM Stakeholders API handler."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_models import (
    ContactListItem,
    CreateStakeholderRequest,
    StakeholderDetail,
    UpdateStakeholderRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm import (
    StakeholdersService,
    StakeholdersServiceError,
)

logger = get_logger(__name__)

stakeholders_router = APIRouter()


async def get_stakeholders_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a StakeholdersService instance for the current org."""
    service = StakeholdersService(session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Stakeholders service close failed", exc_info=True)


@stakeholders_router.get(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[list[StakeholderDetail]],
    summary="Get stakeholders for a deal",
)
async def get_deal_stakeholders(
    deal_id: UUID,
    service: StakeholdersService = Depends(get_stakeholders_service),
):
    """Get all stakeholders associated with a specific deal."""
    try:
        stakeholders = await service.list_stakeholders_by_deal(deal_id)
        return Response.success(data=stakeholders)
    except StakeholdersServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        logger.error(f"Error fetching stakeholders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@stakeholders_router.get(
    "/{deal_id}/stakeholders/available-contacts",
    response_model=GenericResponse[list[ContactListItem]],
    summary="List available contacts for adding as stakeholders",
)
async def list_available_contacts(
    deal_id: UUID,
    query: str | None = None,
    limit: int = 50,
    service: StakeholdersService = Depends(get_stakeholders_service),
):
    """List contacts that can be added as stakeholders to the deal."""
    try:
        contacts = await service.list_available_contacts(deal_id, query, limit)
        return Response.success(data=contacts)
    except StakeholdersServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        logger.error(f"Error listing available contacts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@stakeholders_router.post(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[StakeholderDetail],
    summary="Manually add a stakeholder",
)
async def create_stakeholder(
    deal_id: UUID,
    request: CreateStakeholderRequest,
    service: StakeholdersService = Depends(get_stakeholders_service),
):
    """
    Manually add a stakeholder to a deal.
    """
    try:
        new_stakeholder = await service.create_stakeholder(deal_id, request)
        return Response.success_response("Stakeholder added successfully", new_stakeholder)
    except StakeholdersServiceError as e:
        if e.status_code == 409:
            raise HTTPException(status_code=409, detail=e.message) from e
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to create stakeholder for deal %s", deal_id)
        return Response.internal_server_error_response("Failed to create stakeholder")


@stakeholders_router.patch(
    "/{deal_id}/stakeholders/{stakeholder_id}",
    response_model=GenericResponse[StakeholderDetail],
    summary="Update stakeholder details",
)
async def update_stakeholder(
    deal_id: UUID,
    stakeholder_id: UUID,
    request: UpdateStakeholderRequest,
    service: StakeholdersService = Depends(get_stakeholders_service),
):
    """
    Update manual fields for a stakeholder (Stance, Power, Motivation).
    """
    try:
        stakeholder = await service.update_stakeholder(deal_id, stakeholder_id, request)
        return Response.success_response("Stakeholder updated successfully", stakeholder)
    except StakeholdersServiceError as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail=e.message) from e
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update stakeholder %s for deal %s", stakeholder_id, deal_id)
        return Response.internal_server_error_response("Failed to update stakeholder")
