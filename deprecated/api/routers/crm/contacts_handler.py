"""CRM contacts API handler."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_contact_models import (
    ContactCreateRequest,
    ContactUpdateRequest,
    ContactWriteResponse,
    CRMContactsPage,
)
from src.api.schemas.crm_models import ContactListItem
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.infra.utils.logger import get_logger
from src.service.crm.internal.crm_contact_service import CRMContactService, CRMContactServiceError
from src.workers.deal_worker.client import send_deal_task_async
from src.workers.deal_worker.services.deal_change_detector import StakeholderChangeType

logger = get_logger(__name__)

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

contacts_router = APIRouter()


async def get_contact_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    service = CRMContactService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("CRMContactService close failed", exc_info=True)


def _map_contact_write_response(contact) -> ContactWriteResponse:
    return ContactWriteResponse(
        contact_id=contact.contact_id,
        external_id=contact.external_id,
        first_name=contact.first_name,
        last_name=contact.last_name,
        full_name=contact.full_name,
        email=contact.email,
        job_title=contact.job_title,
        company_name=contact.company_name,
        owner_user_id=contact.owner_id,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
    )


@contacts_router.get(
    "/contacts",
    response_model=GenericResponse[CRMContactsPage],
    tags=["crm", "contacts"],
    summary="List internal CRM contacts",
)
async def list_contacts(
    response: FastAPIResponse,
    limit: int = Query(50, ge=1, le=100, description="Maximum contacts per page"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    q: str | None = Query(None, description="Search by name, email, or company"),
    service: CRMContactService = Depends(get_contact_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMContactsPage]:
    """List internal CRM contacts for the organisation."""
    try:
        contacts, next_cursor = await service.list_contacts(limit=limit, cursor=cursor, q=q)
        items = [
            ContactListItem(
                contact_id=contact.contact_id,
                external_id=contact.external_id,
                first_name=contact.first_name,
                last_name=contact.last_name,
                email=contact.email,
                job_title=contact.job_title,
                company_name=contact.company_name,
            )
            for contact in contacts
        ]
        payload = CRMContactsPage(items=items, cursor=next_cursor)
        return Response.success_response(message="Contacts retrieved successfully", data=payload)
    except CRMContactServiceError as exc:
        logger.warning("Contact list error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing contacts: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.post(
    "/contacts",
    response_model=GenericResponse[ContactWriteResponse],
    status_code=201,
    tags=["crm", "contacts"],
    summary="Create an internal CRM contact",
)
async def create_contact(
    request: ContactCreateRequest,
    response: FastAPIResponse,
    service: CRMContactService = Depends(get_contact_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactWriteResponse]:
    """Create a new internal CRM contact."""
    try:
        contact = await service.create_contact(request.model_dump())
        await service.session.commit()
        await service.session.refresh(contact)
        payload = _map_contact_write_response(contact)
        return Response.created_response(
            message="Contact created successfully", data=payload, response=response
        )
    except CRMContactServiceError as exc:
        await service.session.rollback()
        logger.warning("Contact create error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error creating contact: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.patch(
    "/contacts/{contact_id}",
    response_model=GenericResponse[ContactWriteResponse],
    tags=["crm", "contacts"],
    summary="Update an internal CRM contact",
)
async def update_contact(
    contact_id: UUID,
    request: ContactUpdateRequest,
    response: FastAPIResponse,
    service: CRMContactService = Depends(get_contact_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactWriteResponse]:
    """Update fields for an internal CRM contact."""
    try:
        contact, deal_ids, integration_id = await service.update_contact(
            contact_id, request.model_dump(exclude_unset=True)
        )
        await service.session.commit()
        await service.session.refresh(contact)
        await _trigger_stakeholder_sync(
            organisation_id=service.organisation_id,
            deal_ids=deal_ids,
            integration_id=integration_id,
            change_type=StakeholderChangeType.PARTICIPANT_UPDATED,
        )
        payload = _map_contact_write_response(contact)
        return Response.success_response(message="Contact updated successfully", data=payload)
    except CRMContactServiceError as exc:
        await service.session.rollback()
        logger.warning("Contact update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error updating contact: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.delete(
    "/contacts/{contact_id}",
    response_model=GenericResponse[ContactWriteResponse],
    tags=["crm", "contacts"],
    summary="Delete an internal CRM contact",
)
async def delete_contact(
    contact_id: UUID,
    response: FastAPIResponse,
    service: CRMContactService = Depends(get_contact_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactWriteResponse]:
    """Soft delete an internal CRM contact."""
    try:
        contact, deal_ids, integration_id = await service.delete_contact(contact_id)
        await service.session.commit()
        await service.session.refresh(contact)
        await _trigger_stakeholder_sync(
            organisation_id=service.organisation_id,
            deal_ids=deal_ids,
            integration_id=integration_id,
            change_type=StakeholderChangeType.PARTICIPANT_REMOVED,
        )
        payload = _map_contact_write_response(contact)
        return Response.success_response(message="Contact deleted successfully", data=payload)
    except CRMContactServiceError as exc:
        await service.session.rollback()
        logger.warning("Contact delete error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error deleting contact: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


async def _trigger_stakeholder_sync(
    *,
    organisation_id: UUID,
    deal_ids: list[UUID],
    integration_id: UUID,
    change_type: StakeholderChangeType,
) -> None:
    if not deal_ids:
        return

    for deal_id in deal_ids:
        try:
            await send_deal_task_async(
                "stakeholder.sync_deal_stakeholders",
                kwargs={
                    "organisation_id": str(organisation_id),
                    "deal_id": str(deal_id),
                    "change_type": change_type.value,
                    "integration_id": str(integration_id),
                    "integration_type": IntegrationType.INTERNAL_CRM.value,
                },
                queue="deal",
                routing_key="deal",
            )
        except Exception:
            logger.warning(
                "Failed to trigger stakeholder sync for deal %s",
                deal_id,
                exc_info=True,
            )
