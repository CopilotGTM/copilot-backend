"""Tenant-scoped CRM contact endpoints.

Called by the contacts UI and API clients that manage internal CRM contact records.
The router keeps HTTP concerns here and delegates tenant isolation, validation, and
persistence to org-scoped services built from the current user's organisation.
"""

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
from src.api.presenters.crm.activity_feed_presenter import present_activity_feed_page
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_activity_feed_models import (
    ActivityFeedPage,
    ActivityFeedQueryParams,
)
from src.api.schemas.crm_contact_models import (
    AddContactToDealRequest,
    ContactDealInvolvement,
    ContactDetail,
    ContactListItem,
    ContactMutationItem,
    ContactProfileEnrichment,
    ContactResearchSummary,
    ContactSearchItem,
    ContactSearchPage,
    ContactsPage,
    CreateContactRequest,
    UpdateContactRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.crm import CRMActivityObjectType
from src.infra.utils.logger import get_logger
from src.service.crm.activity_feed_read_service import (
    ActivityFeedReadService,
    ActivityFeedReadServiceError,
)
from src.service.crm.contacts.contracts import (
    ContactCreateCommand,
    ContactDealLinkServiceError,
    ContactReadServiceError,
    ContactWriteContext,
    ContactWriteServiceError,
)
from src.service.crm.contacts.deal_link_service import ContactDealLinkService
from src.service.crm.contacts.read_service import ContactReadService
from src.service.crm.contacts.write_service import ContactWriteService
from src.workers.deal_worker.utils.account_research_dispatch import (
    dispatch_account_research_async,
)
from src.workers.deal_worker.utils.contact_research_dispatch import (
    dispatch_contact_research_async,
)

logger = get_logger("contacts_handler")

# Common error message (Sonar S1192 compliance)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

contacts_router = APIRouter()


async def get_contact_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield a read service bound to the authenticated user's organisation.

    The organisation id comes from auth context, not request parameters, so callers cannot
    use a contact id from another tenant to cross the schema boundary.
    """
    service = ContactReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Contacts service close failed", exc_info=True)


async def get_contact_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield a write service bound to the authenticated user's organisation.

    Mutating routes share this dependency so create/update operations all use the same
    tenant-scoped session and rollback path.
    """
    service = ContactWriteService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Contact write service close failed", exc_info=True)


async def get_contact_deal_link_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield a contact-to-deal linker for the authenticated organisation.

    The service validates both sides of the association inside the org schema before it
    creates a participant row.
    """
    service = ContactDealLinkService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Contact deal-link service close failed", exc_info=True)


async def get_contact_activity_feed_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield an activity feed reader scoped to the authenticated organisation.

    Activity links are object-id based, so the org-scoped session is the trust boundary
    that prevents a contact id from resolving activity in another tenant.
    """
    service = ActivityFeedReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Contact activity-feed service close failed", exc_info=True)


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
    service: ContactReadService = Depends(get_contact_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactsPage]:
    """Return a paginated contact page using only the current organisation's data."""
    try:
        rows, total_count = await service.list_contacts(
            page=page,
            limit=limit,
            q=q,
            company_id=company_id,
        )

        # The read service returns domain rows plus presentation helpers; this route flattens
        # them into the API schema so clients do not need to understand internal joins.
        items = [
            ContactListItem(
                contact_id=row["contact"].contact_id,
                full_name=row["contact"].full_name,
                email=row["contact"].email,
                additional_emails=row["contact"].additional_emails or [],
                job_title=row["contact"].job_title,
                phone=row["contact"].phone,
                mobile_phone=row["contact"].mobile_phone,
                linkedin_url=row["contact"].linkedin_url
                or (row["contact"].properties or {}).get("linkedin_url"),
                company=row.get("company"),
                owner=row.get("owner"),
                deal_count=row["deal_count"],
                last_contacted_at=row["contact"].last_contacted_at,
                last_activity_at=row["contact"].last_activity_at,
                source=row["source"],
                is_profile_enriched=bool(
                    row["contact"].profile_enriched_at or row["contact"].profile_enrichment
                ),
                profile_enriched_at=row["contact"].profile_enriched_at,
                created_at=row["contact"].created_at,
                updated_at=row["contact"].updated_at,
            )
            for row in rows
        ]

        # Empty result sets still return page 1 so the frontend can render pagination controls
        # without special-casing "zero pages".
        total_pages = max(1, math.ceil(total_count / limit)) if total_count > 0 else 1

        data = ContactsPage(
            items=items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Contacts retrieved", data=data)
    except ContactReadServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error listing contacts", exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.get(
    "/contacts/search",
    response_model=GenericResponse[ContactSearchPage],
    summary="Search CRM contacts with cursor pagination",
)
async def search_contacts(
    response: FastAPIResponse,
    q: str | None = Query(None, description="Search by name, email, or company"),
    limit: int = Query(20, ge=1, le=50, description="Maximum contacts per cursor page"),
    cursor: str | None = Query(None, description="Opaque cursor returned by the previous page"),
    company_id: UUID | None = Query(None, description="Filter contacts by company UUID"),
    service: ContactReadService = Depends(get_contact_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactSearchPage]:
    """Return lightweight contact picker rows using cursor pagination."""
    try:
        items_raw, next_cursor = await service.search_contacts(
            q=q,
            limit=limit,
            cursor=cursor,
            company_id=company_id,
        )
        page_data = ContactSearchPage(
            items=[ContactSearchItem(**row) for row in items_raw],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )
        return Response.success_response("Contacts search results retrieved", data=page_data)
    except ContactReadServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Unexpected error searching contacts")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.get(
    "/contacts/search/create-candidates",
    response_model=GenericResponse[ContactSearchPage],
    summary="Search strict contact candidates before create",
)
async def search_contact_create_candidates(
    response: FastAPIResponse,
    q: str | None = Query(None, description="Search by contact name or email"),
    limit: int = Query(20, ge=1, le=50, description="Maximum contacts per cursor page"),
    cursor: str | None = Query(None, description="Opaque cursor returned by the previous page"),
    service: ContactReadService = Depends(get_contact_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactSearchPage]:
    """Return strict person-identity candidates for the create-contact flow."""
    try:
        items_raw, next_cursor = await service.search_contacts_for_create(
            q=q,
            limit=limit,
            cursor=cursor,
        )
        page_data = ContactSearchPage(
            items=[ContactSearchItem(**row) for row in items_raw],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )
        return Response.success_response("Contact create candidates retrieved", data=page_data)
    except ContactReadServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Unexpected error searching contact create candidates")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@contacts_router.post(
    "/contacts",
    response_model=GenericResponse[ContactMutationItem],
    summary="Create a new CRM contact",
)
async def create_contact(
    body: CreateContactRequest,
    response: FastAPIResponse,
    service: ContactWriteService = Depends(get_contact_write_service),
    user_ids: UserIDs = Depends(get_current_user),
    _crm_write_guard: None = Depends(require_crm_write_scope("contacts:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactMutationItem]:
    """Create a manual CRM contact and commit the org-scoped transaction."""
    try:
        result = await service.create_contact(
            ContactCreateCommand(
                email=body.email,
                name=body.name,
                job_title=body.job_title,
                linkedin_url=body.linkedin_url,
            ),
            context=ContactWriteContext(
                source="api",
                actor_user_id=user_ids.user_id,
                actor_name=getattr(user_ids, "user_name", None),
                actor_email=getattr(user_ids, "user_email", None),
                created_via="contacts_app",
            ),
        )
        # The write service stages all changes on the request session; the router commits only
        # after the mutation succeeds so validation failures can cleanly roll back.
        await service.org_session.commit()
        contact = result.contact
        try:
            await dispatch_contact_research_async(
                organisation_id=user_ids.organisation_id,
                contact_id=contact.contact_id,
                user_id=user_ids.user_id,
                trigger="contact_created",
            )
        except Exception:
            logger.warning(
                "Contact research dispatch failed after contact create",
                extra={
                    "event": "contact_research_dispatch_failed",
                    "organisation_id": str(user_ids.organisation_id),
                    "contact_id": str(contact.contact_id),
                },
                exc_info=True,
            )
        if result.created_account_id:
            try:
                await dispatch_account_research_async(
                    organisation_id=user_ids.organisation_id,
                    company_id=result.created_account_id,
                    user_id=user_ids.user_id,
                    trigger="contact_created_account",
                )
            except Exception:
                logger.warning(
                    "Account research dispatch failed after contact-created account",
                    extra={
                        "event": "account_research_dispatch_failed",
                        "organisation_id": str(user_ids.organisation_id),
                        "company_id": str(result.created_account_id),
                        "contact_id": str(contact.contact_id),
                    },
                    exc_info=True,
                )

        data = ContactMutationItem(
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
    except ContactWriteServiceError as e:
        # Service errors are expected validation or conflict failures, but the session may
        # already contain staged changes from the attempted write.
        await service.org_session.rollback()
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await service.org_session.rollback()
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
    service: ContactReadService = Depends(get_contact_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ContactDetail]:
    """Return one contact with company, owner, enrichment, research, and deal context."""
    try:
        result = await service.get_contact_detail(contact_id)
        contact = result["contact"]
        deals = result.get("deals", [])

        # Deal involvement is derived from participant rows so the contact page can show the
        # contact's role in each deal without exposing raw join-table fields.
        deal_involvement = [
            ContactDealInvolvement(
                deal_id=d["deal"].deal_id,
                deal_name=d["deal"].name,
                deal_status=getattr(d["deal"].deal_status, "value", d["deal"].deal_status),
                stage_name=d["deal"].stage_name,
                last_activity_at=d["deal"].last_activity_at,
                participant_id=d["participant"].participant_id,
                relationship_role=d["participant"].relationship_role,
            )
            for d in deals
        ]
        profile_enrichment = contact.profile_enrichment or {}
        research_summary = None
        # A last-researched timestamp without a text summary still matters to clients because it
        # tells them research ran and found no stored narrative.
        if contact.contact_research or contact.last_researched_at:
            research_summary = ContactResearchSummary(
                summary=contact.contact_research,
                last_researched_at=contact.last_researched_at,
            )

        data = ContactDetail(
            contact_id=contact.contact_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            email=contact.email,
            job_title=contact.job_title,
            phone=contact.phone,
            mobile_phone=contact.mobile_phone,
            linkedin_url=contact.linkedin_url or (contact.properties or {}).get("linkedin_url"),
            website=contact.website,
            lifecycle_stage=contact.lifecycle_stage,
            lead_status=contact.lead_status,
            marketing_status=contact.marketing_status,
            company=result.get("company"),
            owner=result.get("owner"),
            deal_count=len(deals),
            source=result.get("source", "manual"),
            created_at=contact.created_at,
            updated_at=contact.updated_at,
            source_created_at=contact.source_created_at,
            source_modified_at=contact.source_modified_at,
            synced_at=contact.synced_at,
            additional_emails=contact.additional_emails or [],
            address=contact.address,
            city=contact.city,
            state=contact.state,
            postal_code=contact.postal_code,
            country=contact.country,
            timezone=contact.timezone,
            last_contacted_at=contact.last_contacted_at,
            last_activity_at=contact.last_activity_at,
            research_summary=research_summary,
            profile_enrichment=ContactProfileEnrichment(
                is_enriched=bool(contact.profile_enriched_at or profile_enrichment),
                source=contact.profile_enrichment_source,
                enriched_at=contact.profile_enriched_at,
                data=profile_enrichment,
            ),
            deal_involvement=deal_involvement,
        )
        return Response.success_response(message="Contact retrieved", data=data)
    except ContactReadServiceError as e:
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


@contacts_router.get(
    "/contacts/{contact_id}/activity-feed",
    response_model=GenericResponse[ActivityFeedPage],
    summary="Get contact activity feed",
)
async def get_contact_activity_feed(
    contact_id: UUID,
    response: FastAPIResponse,
    service: ActivityFeedReadService = Depends(get_contact_activity_feed_service),
    query: ActivityFeedQueryParams = Depends(),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ActivityFeedPage]:
    """Return activity-feed cards linked to this contact through org-local activity links."""
    try:
        # The activity feed service resolves links inside the current org schema; object_id alone
        # is not trusted as an access boundary.
        records, next_cursor, total_count = await service.list_object_activities(
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=contact_id,
            limit=query.limit,
            cursor=query.cursor,
            activity_types=query.activity_types,
            source_types=query.source_types,
            start_date=query.start_date,
            end_date=query.end_date,
            sort_order=query.sort,
        )
        return Response.success_response(
            message="Activity feed retrieved successfully",
            data=present_activity_feed_page(
                object_type=CRMActivityObjectType.CONTACT.value,
                object_id=contact_id,
                records=records,
                total_count=total_count,
                cursor=next_cursor,
            ),
        )
    except ActivityFeedReadServiceError as exc:
        logger.warning("Contact activity feed access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error getting activity feed for contact %s", contact_id)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@contacts_router.patch(
    "/contacts/{contact_id}",
    response_model=GenericResponse[ContactMutationItem],
    summary="Update a contact",
)
async def update_contact(
    contact_id: UUID,
    body: UpdateContactRequest,
    response: FastAPIResponse,
    service: ContactWriteService = Depends(get_contact_write_service),
    user_ids: UserIDs = Depends(get_current_user),
    _crm_write_guard: None = Depends(require_crm_write_scope("contacts:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[ContactMutationItem]:
    """Patch allowed contact fields and return the updated contact projection."""
    try:
        updates = body.model_dump(exclude_unset=True)
        result = await service.update_contact(
            contact_id,
            updates,
            context=ContactWriteContext(
                source="api",
                actor_user_id=user_ids.user_id,
                created_via="contacts_app",
            ),
        )
        # Commit before shaping the response so the returned timestamps and downstream reads
        # reflect the same durable state the client just wrote.
        await service.org_session.commit()
        contact = result.contact

        # Source labels are read through the contact read service because integration display
        # naming lives with read-side CRM presentation logic.
        source = await ContactReadService(
            service.org_session, service.organisation_id
        ).resolve_source_for_contact(contact.integration_id)

        data = ContactMutationItem(
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
    except ContactWriteServiceError as e:
        await service.org_session.rollback()
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await service.org_session.rollback()
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
    service: ContactDealLinkService = Depends(get_contact_deal_link_service),
    user_ids: UserIDs = Depends(get_current_user),
    _crm_write_guard: None = Depends(require_crm_write_scope("deal_participants:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    """Associate a contact with a deal participant record in the current organisation."""
    try:
        link_result = await service.add_contact_to_deal(
            contact_id=contact_id,
            deal_id=body.deal_id,
            relationship_role=body.relationship_role,
            actor_user_id=user_ids.user_id,
        )
        result = {
            "created": link_result.created,
            "skipped": link_result.skipped,
            "contact_participant_changed": link_result.contact_participant_changed,
        }

        # Commit participant records before queueing follow-up work; the worker opens its own
        # session and would miss uncommitted participant rows.
        await service.org_session.commit()

        # Trigger stakeholder sync only for new associations. Existing links are idempotent and
        # should not enqueue duplicate rematching work.
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
                # The contact-to-deal write already succeeded; failing the HTTP request here would
                # make clients retry and risk duplicate association attempts.
                logger.warning(
                    "Failed to trigger stakeholder sync after contact-to-deal addition",
                    exc_info=True,
                )

        return Response.success_response(message="Contact added to deal", data=result)
    except ContactDealLinkServiceError as e:
        await service.org_session.rollback()
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await service.org_session.rollback()
        logger.error(
            "Error adding contact to deal",
            extra={"contact_id": str(contact_id), "deal_id": str(body.deal_id)},
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
