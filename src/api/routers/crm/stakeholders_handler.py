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
    AddContactsAsStakeholdersRequest,
    AddContactsAsStakeholdersResponse,
    AvailableContactItem,
    StakeholderDetail,
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
        return Response.success_response(message="Stakeholders retrieved", data=stakeholders)
    except StakeholdersServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        logger.error(f"Error fetching stakeholders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@stakeholders_router.get(
    "/{deal_id}/stakeholders/available-contacts",
    response_model=GenericResponse[list[AvailableContactItem]],
    summary="List available contacts for adding as stakeholders",
)
async def list_available_contacts(
    deal_id: UUID,
    query: str | None = None,
    limit: int = 50,
    service: StakeholdersService = Depends(get_stakeholders_service),
):
    """
    List contacts that can be added as stakeholders to the deal.

    Only returns contacts that have a valid email address, as email is required
    for stakeholder identification and communication.

    Response includes:
    - Contact identity (name, email)
    - Professional context (job title, company name)
    - CRM metadata for reference
    """
    try:
        contacts = await service.list_available_contacts(deal_id, query, limit)
        return Response.success_response(message="Available contacts retrieved", data=contacts)
    except StakeholdersServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        logger.error(f"Error listing available contacts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@stakeholders_router.post(
    "/{deal_id}/stakeholders/add-from-contacts",
    response_model=GenericResponse[AddContactsAsStakeholdersResponse],
    summary="Add selected contacts as stakeholders",
)
async def add_contacts_as_stakeholders(
    deal_id: UUID,
    request: AddContactsAsStakeholdersRequest,
    service: StakeholdersService = Depends(get_stakeholders_service),
):
    """
    Add selected contacts as stakeholders to a deal.

    Only contacts with valid email addresses will be added. Contacts without
    email addresses are skipped and reported in the response.

    Returns:
    - List of successfully added stakeholders
    - List of skipped contact IDs (no email or already exists)
    """
    try:
        result = await service.add_contacts_as_stakeholders(deal_id, request.contact_ids)
        return Response.success_response("Stakeholders added from contacts", result)
    except StakeholdersServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to add contacts as stakeholders for deal %s", deal_id)
        return Response.internal_server_error_response("Failed to add stakeholders")
