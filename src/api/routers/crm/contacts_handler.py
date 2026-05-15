"""CRM Contacts API handler — tenant-scoped endpoints for accessing and managing contact data."""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_contact_models import (
    AddContactToDealRequest,
    ContactDealInvolvement,
    ContactDetail,
    ContactListItem,
    ContactsPage,
    CreateContactRequest,
    UpdateContactRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.contacts_access_service import (
    ContactsAccessService,
    ContactsAccessServiceError,
)

logger = get_logger("contacts_handler")

# Common error message (Sonar S1192 compliance)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

contacts_router = APIRouter()


async def get_contacts_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a ContactsAccessService instance for the current org."""
    service = ContactsAccessService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Contacts service close failed", exc_info=True)


@contacts_router.get(
    "/contacts",
    response_model=GenericResponse[ContactsPage],
    summary="List CRM contacts with pagination and filtering",
)
async def list_contacts(
    response: FastAPIResponse,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(25, ge=1, le=100, description="Maximum contacts per page"),
    q: str | None = Query(None, description="Case-insensitive search on name, email, or company"),
    company_id: UUID | None = Query(None, description="Filter contacts by company UUID"),
    service: ContactsAccessService = Depends(get_contacts_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactsPage]:
    """List CRM contacts with optional filters and pagination."""
    try:
        rows, total_count = await service.list_contacts(
            page=page,
            limit=limit,
            q=q,
            company_id=company_id,
        )

        items = [
            ContactListItem(
                contact_id=row["contact"].contact_id,
                first_name=row["contact"].first_name,
                last_name=row["contact"].last_name,
                full_name=row["contact"].full_name,
                email=row["contact"].email,
                job_title=row["contact"].job_title,
                phone=row["contact"].phone,
                company_name=row["contact"].company_name,
                company_id=row.get("company_id"),
                company_domain=row.get("company_domain"),
                lifecycle_stage=row["contact"].lifecycle_stage,
                owner_name=row["contact"].owner_name,
                deal_count=row["deal_count"],
                source=row["source"],
                created_at=row["contact"].created_at,
                updated_at=row["contact"].updated_at,
            )
            for row in rows
        ]

        total_pages = max(1, math.ceil(total_count / limit)) if total_count > 0 else 1

        data = ContactsPage(
            items=items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Contacts retrieved", data=data)
    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error listing contacts", exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.post(
    "/contacts",
    response_model=GenericResponse[ContactListItem],
    summary="Create a new CRM contact",
)
async def create_contact(
    body: CreateContactRequest,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("contacts:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactListItem]:
    """Create a new contact in the internal CRM."""
    try:
        extra: dict = {}
        if body.job_title is not None:
            extra["job_title"] = body.job_title
        if body.phone is not None:
            extra["phone"] = body.phone
        if body.linkedin_url is not None:
            extra["properties"] = {"linkedin_url": body.linkedin_url}
        if body.company_id is not None:
            extra["company_id"] = body.company_id

        contact = await service.create_contact(email=body.email, name=body.name, **extra)

        data = ContactListItem(
            contact_id=contact.contact_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            email=contact.email,
            job_title=contact.job_title,
            phone=contact.phone,
            company_name=contact.company_name,
            company_id=contact.company_id,
            company_domain=None,
            lifecycle_stage=contact.lifecycle_stage,
            owner_name=getattr(contact, "owner_name", None),
            deal_count=0,
            source="manual",
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )
        return Response.created_response(message="Contact created", data=data)
    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error creating contact", exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.get(
    "/contacts/{contact_id}",
    response_model=GenericResponse[ContactDetail],
    summary="Get contact detail",
)
async def get_contact_detail(
    contact_id: UUID,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactDetail]:
    """Get full contact detail including deal involvement."""
    try:
        result = await service.get_contact_detail(contact_id)
        contact = result["contact"]
        deals = result.get("deals", [])

        deal_involvement = [
            ContactDealInvolvement(
                deal_id=d["deal"].deal_id,
                deal_name=d["deal"].name,
                deal_status=str(d["deal"].deal_status) if d["deal"].deal_status else None,
                stage_name=d["deal"].stage_name,
                stakeholder_id=d["stakeholder_id"],
                stance=None,
                decision_power=None,
                relationship_role=str(d["relationship_role"])
                if d.get("relationship_role")
                else None,
            )
            for d in deals
        ]

        data = ContactDetail(
            contact_id=contact.contact_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            email=contact.email,
            job_title=contact.job_title,
            phone=contact.phone,
            company_name=contact.company_name,
            company_id=result.get("company_id"),
            company_domain=result.get("company_domain"),
            lifecycle_stage=contact.lifecycle_stage,
            owner_name=getattr(contact, "owner_name", None),
            deal_count=len(deals),
            source=result.get("source", "manual"),
            created_at=contact.created_at,
            updated_at=contact.updated_at,
            additional_emails=contact.additional_emails or [],
            mobile_phone=contact.mobile_phone,
            linkedin_url=contact.properties.get("linkedin_url") if contact.properties else None,
            address=contact.address,
            city=contact.city,
            state=contact.state,
            country=contact.country,
            last_contacted_at=contact.last_contacted_at,
            last_activity_at=contact.last_activity_at,
            deal_involvement=deal_involvement,
        )
        return Response.success_response(message="Contact retrieved", data=data)
    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error(
            "Error fetching contact detail", extra={"contact_id": str(contact_id)}, exc_info=True
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.patch(
    "/contacts/{contact_id}",
    response_model=GenericResponse[ContactListItem],
    summary="Update a contact",
)
async def update_contact(
    contact_id: UUID,
    body: UpdateContactRequest,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("contacts:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactListItem]:
    """Update allowed fields on a contact."""
    try:
        updates = body.model_dump(exclude_unset=True)
        contact = await service.update_contact(contact_id, updates)

        source = await service.resolve_source_for_contact(contact.integration_id)

        data = ContactListItem(
            contact_id=contact.contact_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            email=contact.email,
            job_title=contact.job_title,
            phone=contact.phone,
            company_name=contact.company_name,
            company_id=contact.company_id,
            company_domain=None,
            lifecycle_stage=contact.lifecycle_stage,
            owner_name=getattr(contact, "owner_name", None),
            deal_count=0,
            source=source,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )
        return Response.success_response(message="Contact updated", data=data)
    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error updating contact", extra={"contact_id": str(contact_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.post(
    "/contacts/{contact_id}/add-to-deal",
    response_model=GenericResponse[dict],
    summary="Associate a contact with a deal",
)
async def add_contact_to_deal(
    contact_id: UUID,
    body: AddContactToDealRequest,
    response: FastAPIResponse,
    service: ContactsAccessService = Depends(get_contacts_service),
    user_ids: UserIDs = Depends(get_current_user),
    _crm_write_guard: None = Depends(require_crm_write_scope("deal_participants:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    """Add a contact to a deal as a participant."""
    try:
        result = await service.add_contact_to_deal(
            contact_id=contact_id,
            deal_id=body.deal_id,
            relationship_role=body.relationship_role,
            actor_user_id=user_ids.user_id,
        )

        # Commit participant records so the async worker can see them
        await service.org_session.commit()

        # Trigger stakeholder sync so DealStakeholder records are created
        if result.get("created"):
            try:
                from src.workers.deal_worker.client import send_deal_task_async
                from src.workers.deal_worker.services.deal_change_detector import (
                    StakeholderChangeType,
                )

                await send_deal_task_async(
                    "stakeholder.sync_deal_stakeholders",
                    kwargs={
                        "organisation_id": str(service.organisation_id),
                        "deal_id": str(body.deal_id),
                        "change_type": StakeholderChangeType.PARTICIPANT_ADDED.value,
                        "integration_id": None,
                        "integration_type": None,
                        "trigger_rematching": True,
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to trigger stakeholder sync after contact-to-deal addition",
                    exc_info=True,
                )

        return Response.success_response(message="Contact added to deal", data=result)
    except ContactsAccessServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error(
            "Error adding contact to deal",
            extra={"contact_id": str(contact_id), "deal_id": str(body.deal_id)},
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
