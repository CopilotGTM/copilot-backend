"""CRM Leads API handler — tenant-scoped endpoints for lead management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.presenters.crm.lead_presenter import (
    present_lead_conversion_response,
    present_lead_detail,
    present_lead_list_item,
    present_lead_participant_item,
    present_leads_page,
)
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
from src.domain.models.organisation_tables.sync.crm.leads import CRMLeadSourceType, CRMLeadStatus
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.leads.conversion_service import LeadConversionService
from src.service.crm.leads.participant_service import LeadParticipantService
from src.service.crm.leads.read_service import LeadReadService
from src.service.crm.leads.write_service import LeadWriteService

logger = get_logger("leads_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

leads_router = APIRouter()
require_leads_create_scope = require_crm_write_scope("leads:create")
require_leads_update_scope = require_crm_write_scope("leads:update")
require_leads_convert_scope = require_crm_write_scope("leads:convert")


def _access_actor(user_ids: UserIDs) -> CRMAccessActor:
    return CRMAccessActor(user_ids.user_id, user_ids.user_role, user_ids.user_email)


async def get_lead_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Dependency that yields a LeadReadService instance for the current org."""
    service = LeadReadService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        master_session,
        _access_actor(user_ids),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Leads service close failed", exc_info=True)


async def get_lead_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Dependency that yields a LeadWriteService instance for the current org."""
    service = LeadWriteService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        master_session,
        _access_actor(user_ids),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Lead write service close failed", exc_info=True)


async def get_lead_participant_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Dependency that yields a LeadParticipantService instance for the current org."""
    service = LeadParticipantService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        master_session,
        _access_actor(user_ids),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Lead participant service close failed", exc_info=True)


async def get_lead_conversion_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Dependency that yields a LeadConversionService instance for the current org."""
    service = LeadConversionService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        master_session,
        _access_actor(user_ids),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Lead conversion service close failed", exc_info=True)


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
    service: LeadReadService = Depends(get_lead_read_service),
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
        data = present_leads_page(
            leads=rows,
            page=page,
            limit=limit,
            total_count=total_count,
        )
        return Response.success_response(message="Leads retrieved", data=data)
    except LeadServiceError as e:
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
    service: LeadWriteService = Depends(get_lead_write_service),
    _crm_write_guard: None = Depends(require_leads_create_scope),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadListItem]:
    """Create a lead in the internal CRM."""
    try:
        payload = body.model_dump(exclude_unset=True)
        lead = await service.create_lead(payload)
        return Response.created_response(
            message="Lead created",
            data=present_lead_list_item(lead),
            response=response,
        )
    except LeadServiceError as e:
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
    service: LeadParticipantService = Depends(get_lead_participant_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[LeadDetail]:
    """Get a lead including participants."""
    try:
        lead, participants = await service.get_lead(lead_id)
        return Response.success_response(
            message="Lead retrieved",
            data=present_lead_detail(lead, participants),
        )
    except LeadServiceError as e:
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
    service: LeadWriteService = Depends(get_lead_write_service),
    _crm_write_guard: None = Depends(require_leads_update_scope),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadListItem]:
    """Update mutable lead fields."""
    try:
        lead = await service.update_lead(lead_id, body.model_dump(exclude_unset=True))
        return Response.success_response(message="Lead updated", data=present_lead_list_item(lead))
    except LeadServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error updating lead", extra={"lead_id": str(lead_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@leads_router.post(
    "/leads/{lead_id}/claim",
    response_model=GenericResponse[LeadListItem],
    summary="Claim an unassigned CRM lead",
)
async def claim_lead(
    lead_id: UUID,
    response: FastAPIResponse,
    service: LeadWriteService = Depends(get_lead_write_service),
    _crm_write_guard: None = Depends(require_leads_update_scope),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadListItem]:
    """Atomically make the current user the owner of an unassigned lead."""
    try:
        lead = await service.claim_lead(lead_id)
        return Response.success_response(message="Lead claimed", data=present_lead_list_item(lead))
    except LeadServiceError as e:
        await service.org_session.rollback()
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await service.org_session.rollback()
        logger.error("Error claiming lead", extra={"lead_id": str(lead_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@leads_router.delete(
    "/leads/{lead_id}",
    response_model=GenericResponse[LeadListItem],
    summary="Archive a CRM lead (admin only)",
)
async def archive_lead(
    lead_id: UUID,
    response: FastAPIResponse,
    reason: str | None = Query(None, max_length=255),
    service: LeadWriteService = Depends(get_lead_write_service),
    _crm_write_guard: None = Depends(require_leads_update_scope),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadListItem]:
    """Soft-archive a lead while preserving its history and conversion links."""
    try:
        lead = await service.archive_lead(lead_id, reason)
        return Response.success_response(message="Lead archived", data=present_lead_list_item(lead))
    except LeadServiceError as e:
        await service.org_session.rollback()
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await service.org_session.rollback()
        logger.error("Error archiving lead", extra={"lead_id": str(lead_id)}, exc_info=True)
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
    service: LeadParticipantService = Depends(get_lead_participant_service),
    _crm_write_guard: None = Depends(require_leads_update_scope),
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
            data=present_lead_participant_item(participant),
            response=response,
        )
    except LeadServiceError as e:
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
    service: LeadConversionService = Depends(get_lead_conversion_service),
    _crm_write_guard: None = Depends(require_leads_convert_scope),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[LeadConversionResponse]:
    """Convert a lead into an internal CRM deal."""
    try:
        result = await service.convert_lead(lead_id, body.model_dump(exclude_unset=True))
        return Response.created_response(
            message="Lead converted",
            data=present_lead_conversion_response(result),
            response=response,
        )
    except LeadServiceError as e:
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
