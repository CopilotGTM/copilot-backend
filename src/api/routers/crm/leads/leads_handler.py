"""CRM Leads API handler — tenant-scoped endpoints for lead management."""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_lead_models import (
    ConvertLeadRequest,
    CreateLeadParticipantRequest,
    CreateLeadRequest,
    LeadConversionResponse,
    LeadDetail,
    LeadListItem,
    LeadParticipantItem,
    LeadsPage,
    UpdateLeadRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadSourceType,
    CRMLeadStatus,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.leads_access_service import (
    LeadsAccessService,
    LeadsAccessServiceError,
)

logger = get_logger("leads_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

leads_router = APIRouter()


async def get_leads_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Dependency that yields a LeadsAccessService instance for the current org."""
    service = LeadsAccessService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        master_session,
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Leads service close failed", exc_info=True)


def _lead_item(lead: CRMLead) -> LeadListItem:
    return LeadListItem.model_validate(lead)


def _participant_item(participant: CRMLeadParticipant) -> LeadParticipantItem:
    return LeadParticipantItem.model_validate(participant)


def _lead_detail(lead: CRMLead, participants: list[CRMLeadParticipant]) -> LeadDetail:
    data = LeadDetail.model_validate(lead)
    data.participants = [_participant_item(participant) for participant in participants]
    return data


def _conversion_response(result: dict) -> LeadConversionResponse:
    lead = result["lead"]
    return LeadConversionResponse(
        lead_id=lead.lead_id,
        status=lead.status,
        converted_deal_id=lead.converted_deal_id,
        converted_at=lead.converted_at,
        copied_participant_count=result.get("copied_participant_count", 0),
    )


@leads_router.get(
    "/leads",
    response_model=GenericResponse[LeadsPage],
    summary="List CRM leads with pagination and filtering",
)
async def list_leads(
    response: FastAPIResponse,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(25, ge=1, le=100, description="Maximum leads per page"),
    q: str | None = Query(None, description="Case-insensitive search on name, email, or company"),
    status: CRMLeadStatus | None = Query(None, description="Filter leads by lifecycle status"),
    owner_id: UUID | None = Query(None, description="Filter leads by owner UUID"),
    source_type: CRMLeadSourceType | None = Query(None, description="Filter leads by source type"),
    service: LeadsAccessService = Depends(get_leads_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[LeadsPage]:
    """List CRM leads with optional filters and pagination."""
    try:
        rows, total_count = await service.list_leads(
            page=page,
            limit=limit,
            q=q,
            status=status,
            owner_id=owner_id,
            source_type=source_type,
        )
        total_pages = max(1, math.ceil(total_count / limit)) if total_count > 0 else 1
        data = LeadsPage(
            items=[_lead_item(row) for row in rows],
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Leads retrieved", data=data)
    except LeadsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error listing leads", exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@leads_router.post(
    "/leads",
    response_model=GenericResponse[LeadListItem],
    summary="Create a CRM lead",
)
async def create_lead(
    body: CreateLeadRequest,
    response: FastAPIResponse,
    service: LeadsAccessService = Depends(get_leads_access_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadListItem]:
    """Create a lead in the internal CRM."""
    try:
        payload = body.model_dump(exclude_unset=True)
        lead = await service.create_lead(payload)
        return Response.created_response(
            message="Lead created",
            data=_lead_item(lead),
            response=response,
        )
    except LeadsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error creating lead", exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@leads_router.get(
    "/leads/{lead_id}",
    response_model=GenericResponse[LeadDetail],
    summary="Get CRM lead detail",
)
async def get_lead_detail(
    lead_id: UUID,
    response: FastAPIResponse,
    service: LeadsAccessService = Depends(get_leads_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[LeadDetail]:
    """Get a lead including participants."""
    try:
        lead, participants = await service.get_lead(lead_id)
        return Response.success_response(
            message="Lead retrieved",
            data=_lead_detail(lead, participants),
        )
    except LeadsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error fetching lead detail", extra={"lead_id": str(lead_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@leads_router.patch(
    "/leads/{lead_id}",
    response_model=GenericResponse[LeadListItem],
    summary="Update a CRM lead",
)
async def update_lead(
    lead_id: UUID,
    body: UpdateLeadRequest,
    response: FastAPIResponse,
    service: LeadsAccessService = Depends(get_leads_access_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadListItem]:
    """Update mutable lead fields."""
    try:
        lead = await service.update_lead(lead_id, body.model_dump(exclude_unset=True))
        return Response.success_response(message="Lead updated", data=_lead_item(lead))
    except LeadsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error updating lead", extra={"lead_id": str(lead_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@leads_router.post(
    "/leads/{lead_id}/participants",
    response_model=GenericResponse[LeadParticipantItem],
    summary="Add a participant to a CRM lead",
)
async def add_lead_participant(
    lead_id: UUID,
    body: CreateLeadParticipantRequest,
    response: FastAPIResponse,
    service: LeadsAccessService = Depends(get_leads_access_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadParticipantItem]:
    """Add a participant link to a lead."""
    try:
        participant = await service.add_participant(
            lead_id,
            body.model_dump(exclude_unset=True),
        )
        return Response.created_response(
            message="Lead participant created",
            data=_participant_item(participant),
            response=response,
        )
    except LeadsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error(
            "Error adding lead participant",
            extra={"lead_id": str(lead_id)},
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@leads_router.post(
    "/leads/{lead_id}/convert",
    response_model=GenericResponse[LeadConversionResponse],
    summary="Convert a CRM lead into a deal",
)
async def convert_lead(
    lead_id: UUID,
    body: ConvertLeadRequest,
    response: FastAPIResponse,
    service: LeadsAccessService = Depends(get_leads_access_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadConversionResponse]:
    """Convert a lead into an internal CRM deal."""
    try:
        result = await service.convert_lead(lead_id, body.model_dump(exclude_unset=True))
        return Response.created_response(
            message="Lead converted",
            data=_conversion_response(result),
            response=response,
        )
    except LeadsAccessServiceError as e:
        await service.org_session.rollback()
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await service.org_session.rollback()
        logger.error("Error converting lead", extra={"lead_id": str(lead_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
