"""CRM Deals API handler - tenant-scoped endpoints for accessing deal data."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Response as FastAPIResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.crm_record_access import (
    require_deal_collaborate,
    require_deal_view,
)
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.presenters.crm.activity_feed_presenter import present_activity_feed_page
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_activity_feed_models import ActivityFeedPage, ActivityFeedQueryParams
from src.api.schemas.crm_deal_write_models import (
    DealAccountUpdateRequest,
    DealCloseRequest,
    DealCreateRequest,
    DealOwnerAssignRequest,
    DealPrimaryContactUpdateRequest,
    DealReopenRequest,
    DealStageMoveRequest,
    DealUpdateRequest,
    DealWriteResponse,
)
from src.api.schemas.crm_models import (
    DealAccountDetail,
    DealCustomColumnDefinition,
    DealDetail,
    DealJobResponse,
    DealListItem,
    DealOwnerDetail,
    DealOwnerFilterItem,
    DealOwnerFilters,
    DealPrimaryContactDetail,
    DealSearchItem,
    DealSearchPage,
    DealsListParams,
    DealsPage,
    DealsStats,
    DealSummaryJobsStatus,
    DealTeamMember,
)
from src.api.schemas.crm_schema_models import CRMEntitySchemaResponse
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.activities import CRMActivityObjectType
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldEntity
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.activity_feed_read_service import (
    ActivityFeedReadService,
    ActivityFeedReadServiceError,
)
from src.service.crm.deals.contracts import DealReadServiceError, DealWriteServiceError
from src.service.crm.deals.last_activity_service import LAST_ACTIVITY_SOURCE_PROPERTY
from src.service.crm.deals.read_service import (
    DealReadService,
    extract_deal_health,
    extract_deal_health_reason,
)
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.jobs.deal_job_service import DealJobService, DealJobServiceError
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService
from src.service.crm.schema.deal_schema_service import DealSchemaService
from sse_starlette.sse import EventSourceResponse

logger = get_logger("deals_handler")
require_deals_update_scope = require_crm_write_scope("deals:update")
GENERIC_DEAL_STREAM_JOB_TYPES = frozenset(
    {
        DealJobType.STAKEHOLDER_SYNC,
        DealJobType.DEAL_COMMUNICATION_HYDRATION,
    }
)
DEAL_SUMMARY_STREAM_JOB_TYPES = frozenset({DealJobType.DEAL_SUMMARY})

# Common error message (Sonar S1192 compliance)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deals_router = APIRouter()

_INTERNAL_DEAL_RESERVED_PROPERTIES = {
    "is_internal_crm",
    "created_via",
    "updated_via",
    "deleted_via",
    "deal_external_ref",
    "import_source_ref",
    LAST_ACTIVITY_SOURCE_PROPERTY,
}
_CURRENCY_SYMBOL_BY_CODE = {
    "USD": "$",
    "INR": "₹",
}
_CURRENCY_CODE_PATTERN = re.compile(r"\b[A-Z]{3}\b")
_CURRENCY_CODE_BY_SYMBOL = {
    "$": "USD",
    "₹": "INR",
}


def _job_stream_response(sse_service: DealJobSSEService, request: Request) -> EventSourceResponse:
    """Return one resumable SSE response and close its Redis resources on exit."""
    last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
    resume_from_id = last_event_id or None

    async def event_generator():
        """Stream frames from the SSE service and always close service resources."""
        try:
            async for event in sse_service.stream_events(resume_from_id=resume_from_id):
                yield event
        finally:
            try:
                await sse_service.close()
            except Exception:
                # A close failure cannot change an HTTP stream that has already ended.
                logger.debug("DealJobSSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())


def _clean_string(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_currency_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not normalized:
        return None

    if normalized in _CURRENCY_SYMBOL_BY_CODE:
        return normalized

    if normalized in _CURRENCY_CODE_BY_SYMBOL:
        return _CURRENCY_CODE_BY_SYMBOL[normalized]

    code_match = _CURRENCY_CODE_PATTERN.search(normalized)
    if code_match:
        candidate = code_match.group(0)
        if candidate in _CURRENCY_SYMBOL_BY_CODE:
            return candidate

    return normalized


def _currency_symbol_for(value: str | None) -> str | None:
    code = _normalize_currency_code(value)
    if code is None:
        return None
    return _CURRENCY_SYMBOL_BY_CODE.get(code)


def _normalize_deal_status(value) -> str:
    if value is None:
        return "open"
    return getattr(value, "value", value)


def get_deals_list_params(
    limit: int = Query(40, ge=1, le=100, description="Maximum deals per page"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    statuses: list[CRMDealStatus] | None = Query(None, description="Filter by deal statuses"),
    pipeline_ids: list[UUID] | None = Query(None, description="Filter by pipeline IDs"),
    owner_ids: list[UUID] | None = Query(None, description="Filter by internal owner IDs"),
    min_amount: float | None = Query(None, description="Minimum deal amount"),
    max_amount: float | None = Query(None, description="Maximum deal amount"),
    min_close_date: datetime | None = Query(None, description="Minimum actual close date"),
    max_close_date: datetime | None = Query(None, description="Maximum actual close date"),
    min_expected_close_date: datetime | None = Query(
        None, description="Minimum expected close date"
    ),
    max_expected_close_date: datetime | None = Query(
        None, description="Maximum expected close date"
    ),
    q: str | None = Query(None, description="Case-insensitive search on deal or company name"),
    sort_by: str | None = Query(
        None,
        description=(
            "Sort field: name, stage, amount, expected_close_date, owner, or last_activity_at."
        ),
    ),
    sort_order: str | None = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
    custom_column_keys: list[str] | None = Query(
        None,
        description="Active internal custom field keys to include in each list row.",
    ),
) -> DealsListParams:
    """Dependency that parses and returns deals list query parameters."""
    if custom_column_keys and len(custom_column_keys) > 50:
        raise HTTPException(
            status_code=422,
            detail="At most 50 custom columns can be requested",
        )
    return DealsListParams(
        limit=limit,
        page=page,
        statuses=statuses,
        pipeline_ids=pipeline_ids,
        owner_ids=owner_ids,
        min_amount=min_amount,
        max_amount=max_amount,
        min_close_date=min_close_date,
        max_close_date=max_close_date,
        min_expected_close_date=min_expected_close_date,
        max_expected_close_date=max_expected_close_date,
        q=q,
        sort_by=sort_by,
        sort_order=sort_order,
        custom_column_keys=custom_column_keys,
    )


async def get_deals_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealReadService instance for the current org."""
    actor = CRMAccessActor(user_ids.user_id, user_ids.user_role, user_ids.user_email)
    service = DealReadService(org_session, user_ids.organisation_id, actor=actor)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Deals service close failed", exc_info=True)


async def get_deal_activity_feed_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield an activity-feed reader scoped to the authenticated organisation."""
    service = ActivityFeedReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Deal activity-feed service close failed", exc_info=True)


async def get_deal_job_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealJobService instance for deal-level jobs."""
    service = DealJobService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealJobService close failed", exc_info=True)


async def get_deal_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Dependency that yields a DealWriteService instance for the current org.

    Uses both org session (for CRM data) and master session (for public.users).
    """
    service = DealWriteService(
        org_session=org_session,
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        actor_user_id=user_ids.user_id,
        access_actor=CRMAccessActor(
            user_ids.user_id,
            user_ids.user_role,
            user_ids.user_email,
        ),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealWriteService close failed", exc_info=True)


async def get_deal_schema_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields deal schema metadata for the current org."""
    service = DealSchemaService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealSchemaService close failed", exc_info=True)


async def _get_integration_types_map(
    session: AsyncSession, organisation_id: UUID, integration_ids: set[UUID]
) -> dict[UUID, bool]:
    """Batch fetch integration types and return a mapping of integration_id -> is_internal.

    Args:
        session: Async database session
        organisation_id: Organisation UUID for filtering
        integration_ids: Set of integration IDs to fetch

    Returns:
        Dict mapping integration_id to is_internal boolean
    """
    if not integration_ids:
        return {}

    stmt = select(KnowledgeIntegration).where(
        col(KnowledgeIntegration.organisation_id) == organisation_id,
        col(KnowledgeIntegration.integration_id).in_(list(integration_ids)),
    )
    result = await session.execute(stmt)
    integrations = result.scalars().all()

    # Build an org-scoped lookup so job visibility and data hydration only run
    # for internal-CRM deals; external integrations must not reuse internal paths.
    return {
        integration.integration_id: integration.integration_type == IntegrationType.INTERNAL_CRM
        for integration in integrations
    }


def _primary_contact_details_from_deal(deal) -> list[DealPrimaryContactDetail]:
    if not any(
        [
            getattr(deal, "primary_contact_id", None),
            getattr(deal, "primary_contact_name", None),
            getattr(deal, "primary_contact_email", None),
            getattr(deal, "primary_contact_phone_number", None),
        ]
    ):
        return []
    return [
        DealPrimaryContactDetail(
            contact_id=getattr(deal, "primary_contact_id", None),
            name=getattr(deal, "primary_contact_name", None),
            email=getattr(deal, "primary_contact_email", None),
            phone_number=getattr(deal, "primary_contact_phone_number", None),
        )
    ]


def _map_deal_write_response(
    deal,
    *,
    primary_contact_details: list[DealPrimaryContactDetail] | None = None,
) -> DealWriteResponse:
    owner_details = (
        [
            DealOwnerDetail(
                owner_id=deal.owner_id,
                name=getattr(deal, "owner_name", None),
                email=getattr(deal, "owner_email", None),
            )
        ]
        if any(
            [
                getattr(deal, "owner_id", None),
                getattr(deal, "owner_name", None),
                getattr(deal, "owner_email", None),
            ]
        )
        else []
    )
    return DealWriteResponse(
        deal_id=deal.deal_id,
        name=deal.name,
        amount=getattr(deal, "amount", None),
        predicted_amount=getattr(deal, "predicted_amount", None),
        currency=_normalize_currency_code(getattr(deal, "currency", None)),
        currency_symbol=_currency_symbol_for(getattr(deal, "currency", None)),
        pipeline_id=deal.pipeline_id,
        pipeline_name=getattr(deal, "pipeline_name", None),
        pipeline_label=getattr(deal, "pipeline_label", None),
        stage_id=deal.stage_id,
        stage_name=getattr(deal, "stage_name", None),
        stage_label=getattr(deal, "stage_label", None),
        deal_status=_normalize_deal_status(deal.deal_status),
        probability=getattr(deal, "probability", None),
        forecast_category=getattr(deal, "forecast_category", None),
        owner_user_id=deal.owner_id,
        owner_name=getattr(deal, "owner_name", None),
        owner_email=getattr(deal, "owner_email", None),
        owner_details=owner_details,
        company_id=getattr(deal, "company_id", None),
        company_name=getattr(deal, "company_name", None),
        company_domain=getattr(deal, "company_domain", None),
        company_industry=getattr(deal, "company_industry", None),
        company_website=getattr(deal, "company_website", None),
        company_linkedin_url=getattr(deal, "company_linkedin_url", None),
        company_country=getattr(deal, "company_country", None),
        primary_contact_id=getattr(deal, "primary_contact_id", None),
        primary_contact_name=getattr(deal, "primary_contact_name", None),
        primary_contact_email=getattr(deal, "primary_contact_email", None),
        primary_contact_details=(
            primary_contact_details
            if primary_contact_details is not None
            else _primary_contact_details_from_deal(deal)
        ),
        source_created_at=getattr(deal, "source_created_at", None),
        close_date=getattr(deal, "close_date", None),
        expected_close_date=getattr(deal, "expected_close_date", None),
        closed_won_at=getattr(deal, "closed_won_at", None),
        closed_lost_at=getattr(deal, "closed_lost_at", None),
        closed_reason=getattr(deal, "closed_reason", None),
        stage_entered_at=getattr(deal, "stage_entered_at", None),
        stage_updated_at=getattr(deal, "stage_updated_at", None),
        deal_type=getattr(deal, "deal_type", None),
        priority=getattr(deal, "priority", None),
        lead_source=getattr(deal, "lead_source", None),
        description=getattr(deal, "description", None),
        created_at=deal.created_at,
        updated_at=deal.updated_at,
        synced_at=getattr(deal, "synced_at", None),
    )


async def _contact_research_job_response(
    *,
    organisation_id: UUID,
    contact_id: UUID,
    user_id: UUID | None,
) -> DealJobResponse | None:
    """Dispatch contact-owned research for contacts attached during deal creation."""
    try:
        from src.workers.deal_worker.utils.contact_research_dispatch import (
            dispatch_contact_research_for_contact,
        )

        job_id = await dispatch_contact_research_for_contact(
            organisation_id=organisation_id,
            contact_id=contact_id,
            user_id=user_id,
            trigger="stakeholder_created",
        )
        if not job_id:
            return None
        return DealJobResponse(
            job_id=UUID(job_id),
            job_type="contact_research",
            status="pending",
        )
    except Exception:
        logger.error(
            "Failed to dispatch inline contact research",
            extra={
                "organisation_id": str(organisation_id),
                "contact_id": str(contact_id),
            },
            exc_info=True,
        )
        return None


def _extract_custom_columns(
    properties: dict[str, Any] | None,
    *,
    is_internal: bool,
    known_keys: list[str] | None = None,
    include_empty: bool = True,
) -> dict[str, Any] | None:
    """Return internal custom columns, optionally including known keys with null values."""
    if not is_internal:
        return None

    props = properties if isinstance(properties, dict) else {}
    custom_columns: dict[str, Any] = {}
    if include_empty and known_keys:
        for key in known_keys:
            if key in _INTERNAL_DEAL_RESERVED_PROPERTIES:
                continue
            custom_columns[key] = props.get(key)

    for key, value in props.items():
        if key in _INTERNAL_DEAL_RESERVED_PROPERTIES:
            continue
        if value is None and not include_empty:
            continue
        if key in custom_columns:
            if value is not None:
                custom_columns[key] = value
        else:
            custom_columns[key] = value

    if not include_empty:
        custom_columns = {key: value for key, value in custom_columns.items() if value is not None}

    return custom_columns or None


@deals_router.get(
    "/schema",
    response_model=GenericResponse[CRMEntitySchemaResponse],
    tags=["crm", "schema"],
    summary="Get CRM schema metadata",
)
async def get_crm_schema(
    response: FastAPIResponse,
    entity: CRMCustomFieldEntity = Query(
        CRMCustomFieldEntity.DEAL,
        description="CRM entity to describe. Currently only deal is supported.",
    ),
    service: DealSchemaService = Depends(get_deal_schema_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMEntitySchemaResponse]:
    """Return the canonical backend-owned CRM schema metadata contract."""
    if entity != CRMCustomFieldEntity.DEAL:
        return Response.error_response(
            message="Unsupported CRM schema entity",
            status_code=400,
            response=response,
        )
    try:
        schema = await service.get_schema()
        return Response.success_response(message="CRM schema retrieved", data=schema)
    except Exception as exc:
        logger.error("Unexpected CRM schema error: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals",
    response_model=GenericResponse[DealsPage],
    tags=["crm", "deals"],
    summary="List CRM deals with pagination and filtering",
    description="""
    Retrieve a list of CRM deals for the current organisation.

    **Pagination:**
    - Page-based pagination using `page` and `limit`

    **Filters (work with both modes):**
    - Deal status (OPEN, WON, LOST, CLOSED)
    - Pipeline ID
    - Owner ID (internal)
    - Deal Amount range (min/max)
    - Actual Close Date range (min/max)
    - Expected Close Date range (min/max)
    - Search query on deal or company name (case-insensitive)
    """,
)
@deals_router.get(
    "/deals/",
    response_model=GenericResponse[DealsPage],
    tags=["crm", "deals"],
    include_in_schema=False,  # Avoid duplicate in OpenAPI docs
)
async def list_deals(
    response: FastAPIResponse,
    params: DealsListParams = Depends(get_deals_list_params),
    service: DealReadService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealsPage]:
    """List CRM deals with optional filters and pagination.

    Returns:
        GenericResponse containing DealsPage with deals list and pagination metadata
    """
    try:
        deals, total_count = await service.list_deals(
            limit=params.limit,
            page=params.page,
            statuses=params.statuses,
            pipeline_ids=params.pipeline_ids,
            owner_ids=params.owner_ids,
            min_amount=params.min_amount,
            max_amount=params.max_amount,
            min_close_date=params.min_close_date,
            max_close_date=params.max_close_date,
            min_expected_close_date=params.min_expected_close_date,
            max_expected_close_date=params.max_expected_close_date,
            q=params.q,
            sort_by=params.sort_by,
            sort_order=params.sort_order,
        )

        # Fetch deal team members (owners, collaborators, split_owners) in one batch query
        deal_ids = [deal.deal_id for deal in deals]
        team_by_deal = await service.get_deal_team_members_batch(deal_ids) if deal_ids else {}

        # Batch fetch integration types to determine is_internal
        integration_ids = {deal.integration_id for deal in deals if deal.integration_id}
        integration_types_map = await _get_integration_types_map(
            service.session, service.organisation_id, integration_ids
        )

        selected_custom_keys: list[str] = []
        if params.custom_column_keys:
            active_custom_keys = set(
                await DealSchemaService(
                    service.session,
                    service.organisation_id,
                ).active_internal_custom_field_keys()
            )
            selected_custom_keys = list(
                dict.fromkeys(key for key in params.custom_column_keys if key in active_custom_keys)
            )

        # Convert to response models
        items: list[DealListItem] = [
            DealListItem(
                deal_id=deal.deal_id,
                name=deal.name,
                amount=deal.amount,
                predicted_amount=deal.predicted_amount,
                currency=_normalize_currency_code(deal.currency),
                currency_symbol=_currency_symbol_for(deal.currency),
                deal_status=_normalize_deal_status(deal.deal_status),
                probability=deal.probability,
                close_date=deal.close_date,
                expected_close_date=deal.expected_close_date,
                stage_id=deal.stage_id,
                stage_name=deal.stage_name,
                stage_label=deal.stage_label,
                pipeline_id=deal.pipeline_id,
                pipeline_name=deal.pipeline_name,
                pipeline_label=deal.pipeline_label,
                owner_details=(
                    [
                        DealOwnerDetail(
                            name=deal.owner_name,
                            email=deal.owner_email,
                            owner_id=deal.owner_id,
                        )
                    ]
                    if any([getattr(deal, "owner_id", None), deal.owner_name, deal.owner_email])
                    else []
                ),
                company_id=deal.company_id,
                company_name=getattr(deal, "company_name", None),
                company_domain=getattr(deal, "company_domain", None),
                account_details=(
                    [
                        DealAccountDetail(
                            company_id=deal.company_id,
                            name=getattr(deal, "company_name", None),
                            domain=getattr(deal, "company_domain", None),
                        )
                    ]
                    if any(
                        [
                            getattr(deal, "company_id", None),
                            getattr(deal, "company_name", None),
                            getattr(deal, "company_domain", None),
                        ]
                    )
                    else []
                ),
                primary_contact_id=getattr(deal, "primary_contact_id", None),
                primary_contact_name=getattr(deal, "primary_contact_name", None),
                primary_contact_email=getattr(deal, "primary_contact_email", None),
                primary_contact_details=(
                    [
                        DealPrimaryContactDetail(
                            contact_id=getattr(deal, "primary_contact_id", None),
                            name=getattr(deal, "primary_contact_name", None),
                            email=getattr(deal, "primary_contact_email", None),
                            phone_number=getattr(deal, "primary_contact_phone_number", None),
                        )
                    ]
                    if any(
                        [
                            getattr(deal, "primary_contact_id", None),
                            getattr(deal, "primary_contact_name", None),
                            getattr(deal, "primary_contact_email", None),
                        ]
                    )
                    else []
                ),
                last_activity_at=getattr(deal, "last_activity_at", None),
                deal_team=[
                    DealTeamMember(
                        participant_id=member.get("participant_id"),
                        user_id=member.get("user_id"),
                        name=member.get("name"),
                        email=member.get("email"),
                        role=member.get("role", "member"),
                    )
                    for member in team_by_deal.get(deal.deal_id, [])
                ],
                is_internal=integration_types_map.get(deal.integration_id, False),
                created_at=deal.created_at,
                updated_at=deal.updated_at,
                synced_at=deal.synced_at,
                custom_columns=(
                    {key: (deal.properties or {}).get(key) for key in selected_custom_keys} or None
                    if integration_types_map.get(deal.integration_id, False)
                    else None
                ),
            )
            for deal in deals
        ]

        total_pages = (total_count + params.limit - 1) // params.limit if params.limit else 0
        page_response = DealsPage(
            items=items,
            page=params.page,
            limit=params.limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response(message="Deals retrieved successfully", data=page_response)

    except DealReadServiceError as e:
        logger.warning("Deals access error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
    except Exception as e:
        logger.error("Unexpected error listing deals: %s", e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals/search",
    response_model=GenericResponse[DealSearchPage],
    tags=["crm", "deals"],
    summary="Search CRM deals with cursor pagination",
)
async def search_deals(
    response: FastAPIResponse,
    q: str | None = Query(None, description="Case-insensitive search on deal or company name"),
    limit: int = Query(20, ge=1, le=50, description="Maximum deals per cursor page"),
    cursor: str | None = Query(None, description="Opaque cursor returned by the previous page"),
    service: DealReadService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealSearchPage]:
    """Return lightweight deal rows for search dropdowns."""
    try:
        deals, next_cursor = await service.search_deals(q=q, limit=limit, cursor=cursor)
        integration_ids = {deal.integration_id for deal in deals if deal.integration_id}
        integration_types_map = await _get_integration_types_map(
            service.session, service.organisation_id, integration_ids
        )
        items = [
            DealSearchItem(
                deal_id=deal.deal_id,
                name=deal.name,
                amount=deal.amount,
                predicted_amount=deal.predicted_amount,
                currency=_normalize_currency_code(deal.currency),
                currency_symbol=_currency_symbol_for(deal.currency),
                deal_status=_normalize_deal_status(deal.deal_status),
                stage_id=deal.stage_id,
                stage_name=deal.stage_name,
                pipeline_id=deal.pipeline_id,
                pipeline_name=deal.pipeline_name,
                company_id=deal.company_id,
                company_name=getattr(deal, "company_name", None),
                company_domain=getattr(deal, "company_domain", None),
                is_internal=integration_types_map.get(deal.integration_id, False),
                owner_details=(
                    [
                        DealOwnerDetail(
                            name=deal.owner_name,
                            email=deal.owner_email,
                            owner_id=deal.owner_id,
                        )
                    ]
                    if any([getattr(deal, "owner_id", None), deal.owner_name, deal.owner_email])
                    else []
                ),
                created_at=deal.created_at,
                updated_at=deal.updated_at,
            )
            for deal in deals
        ]
        page = DealSearchPage(
            items=items,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )
        return Response.success_response(message="Deals search results retrieved", data=page)
    except DealReadServiceError as e:
        logger.warning("Deal search access error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
    except Exception as e:
        logger.error("Unexpected error searching deals: %s", e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals/owners",
    response_model=GenericResponse[DealOwnerFilters],
    tags=["crm", "deals"],
    summary="List distinct deal owners for filters",
    description="""
    Retrieve a distinct list of owners across all CRM deals for the current organisation.

    This endpoint is designed for filter dropdowns and is independent of deal pagination.
    Returns internal owner IDs for filtering.
    """,
)
async def list_deal_owners(
    response: FastAPIResponse,
    limit: int = Query(
        200,
        ge=0,
        le=1000,
        description="Maximum searched owners to return; use 0 for selected-owner hydration only",
    ),
    q: str | None = Query(None, description="Search by owner name, email, or owner UUID"),
    owner_ids: list[UUID] | None = Query(
        None,
        description="Owner IDs to hydrate even if they are outside the current search result.",
    ),
    service: DealReadService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealOwnerFilters]:
    """List distinct deal owners for filter dropdowns."""
    try:
        if owner_ids and len(owner_ids) > 100:
            return Response.error_response(
                message="A maximum of 100 owner_ids can be hydrated at once",
                status_code=400,
                response=response,
            )

        owners = await service.list_deal_owners(q=q, limit=limit, owner_ids=owner_ids)
        items = [
            DealOwnerFilterItem(
                owner_id=owner["owner_id"],
                name=owner.get("name"),
                email=owner.get("email"),
            )
            for owner in owners
        ]
        payload = DealOwnerFilters(items=items)
        return Response.success_response(message="Deal owners retrieved successfully", data=payload)
    except DealReadServiceError as e:
        logger.warning("Deal owners access error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
    except Exception as e:
        logger.error("Unexpected error listing deal owners: %s", e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals/owners/me",
    response_model=GenericResponse[DealOwnerFilters],
    tags=["crm", "deals"],
    summary="Resolve current app user to CRM deal owners",
    description="""
    Resolve the authenticated app user to one or more CRM deal owner IDs.

    Admin deal lists use these IDs for the default "My deals" view. Members use
    them only for the optional personal-owner avatar; their default visibility
    remains scoped by ownership, creation, and participation.
    """,
)
async def get_current_user_deal_owners(
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: DealReadService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealOwnerFilters]:
    """Resolve the current app user to personal CRM owner filters."""
    try:
        owners = await service.resolve_current_user_deal_owners(
            user_id=user_ids.user_id,
            user_email=user_ids.user_email,
        )
        items = [
            DealOwnerFilterItem(
                owner_id=owner["owner_id"],
                name=owner.get("name"),
                email=owner.get("email"),
            )
            for owner in owners
        ]
        payload = DealOwnerFilters(items=items)
        return Response.success_response(
            message="Current user deal owners resolved successfully",
            data=payload,
        )
    except DealReadServiceError as e:
        logger.warning("Current user deal owner resolution error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
    except Exception as e:
        logger.error("Unexpected error resolving current user deal owners: %s", e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals/stats",
    response_model=GenericResponse[DealsStats],
    tags=["crm", "deals"],
    summary="Get aggregate statistics for deals",
    description="""
    Calculate and return aggregate statistics for all CRM deals in the organisation.

    Includes:
    - Total deal count
    - Count by status (OPEN, WON, LOST, CLOSED)
    - Count by pipeline
    - Total and average deal amounts
    """,
)
async def get_deals_stats(
    response: FastAPIResponse,
    service: DealReadService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealsStats]:
    """Calculate and return aggregate statistics for deals.

    Returns:
        GenericResponse containing DealsStats with aggregated metrics
    """
    try:
        stats = await service.get_deals_stats()
        stats_model = DealsStats(**stats)
        return Response.success_response(
            message="Statistics retrieved successfully", data=stats_model
        )

    except DealReadServiceError as e:
        logger.warning("Deals stats error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
    except Exception as e:
        logger.error("Unexpected error calculating stats: %s", e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals/{deal_id}/activity-feed",
    response_model=GenericResponse[ActivityFeedPage],
    tags=["crm", "deals"],
    summary="Get deal activity feed",
)
async def get_deal_activity_feed(
    deal_id: UUID,
    response: FastAPIResponse,
    service: ActivityFeedReadService = Depends(get_deal_activity_feed_service),
    access_service: DealReadService = Depends(get_deals_access_service),
    query: ActivityFeedQueryParams = Depends(),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ActivityFeedPage]:
    """Return activity-feed cards linked to this deal through org-local links."""
    try:
        if await access_service.get_deal_by_id(deal_id) is None:
            return Response.error_response(
                message="Deal not found",
                status_code=404,
                response=response,
            )
        # The org-scoped reader is the tenant boundary; a caller-supplied deal id
        # cannot resolve links from another organisation after deal visibility is checked.
        records, next_cursor, total_count = await service.list_object_activities(
            object_type=CRMActivityObjectType.DEAL.value,
            object_id=deal_id,
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
                object_type=CRMActivityObjectType.DEAL.value,
                object_id=deal_id,
                records=records,
                total_count=total_count,
                cursor=next_cursor,
            ),
        )
    except ActivityFeedReadServiceError as exc:
        logger.warning("Deal activity feed access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except DealReadServiceError as exc:
        logger.warning("Deal activity feed visibility error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error getting activity feed for deal %s", deal_id)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals/{deal_id}",
    response_model=GenericResponse[DealDetail],
    tags=["crm", "deals"],
    summary="Get detailed information for a specific deal",
    description="""
    Retrieve full details for a single CRM deal by its ID.

    Returns all available fields including:
    - Core business context (amount, status, probability)
    - Sales process details (stage, pipeline, forecast)
    - Ownership and associations
    - Activity and history context
    - Custom properties and metrics
    - Deal summary data (if generated)
    - Active deal-level jobs (for SSE reconnection)
    """,
)
async def get_deal(
    deal_id: UUID,
    response: FastAPIResponse,
    service: DealReadService = Depends(get_deals_access_service),
    deal_job_service: DealJobService = Depends(get_deal_job_service),
    include_empty_custom_columns: bool = Query(
        True,
        description=(
            "Include all active custom field keys with null values for missing entries. "
            "Set false to return only populated custom values."
        ),
    ),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealDetail]:
    """Retrieve detailed information for a single deal.

    Args:
        deal_id: UUID of the deal to retrieve

    Returns:
        GenericResponse containing DealDetail with streamlined deal information.
    """
    try:
        deal = await service.get_deal_by_id(deal_id)

        if not deal:
            return Response.error_response(
                message="Deal not found",
                status_code=404,
                response=response,
            )

        # Fetch deal team members (owners, collaborators, split_owners)
        team_by_deal = await service.get_deal_team_members_batch([deal_id])
        deal_team_data = team_by_deal.get(deal_id, [])

        # Build owner_details list
        owner_details = (
            [
                DealOwnerDetail(
                    name=deal.owner_name,
                    email=deal.owner_email,
                    owner_id=deal.owner_id,
                )
            ]
            if any([getattr(deal, "owner_id", None), deal.owner_name, deal.owner_email])
            else []
        )

        # Build deal_team list
        deal_team = [
            DealTeamMember(
                participant_id=member.get("participant_id"),
                user_id=member.get("user_id"),
                name=member.get("name"),
                email=member.get("email"),
                role=member.get("role", "member"),
            )
            for member in deal_team_data
        ]

        # Extract deal health from summary data
        deal_health = extract_deal_health(getattr(deal, "deal_summary_data", None))
        deal_health_reason = extract_deal_health_reason(getattr(deal, "deal_summary_data", None))

        # Calculate deal age in days
        deal_age_days = None
        source_created_at = deal.source_created_at
        if source_created_at is not None:
            created_at = source_created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            deal_age_days = (datetime.now(UTC) - created_at).days

        # Prefer the linked company row, but keep any profile snapshot already
        # carried by the deal so detail matches write responses after account edits.
        company_industry = getattr(deal, "company_industry", None)
        company_website = getattr(deal, "company_website", None)
        company_linkedin_url = getattr(deal, "company_linkedin_url", None)
        company_country = getattr(deal, "company_country", None)
        if deal.company_id:
            company = await service.get_company_by_id(deal.company_id)
            if company:
                company_industry = company.industry
                company_website = company.website
                company_linkedin_url = company.linkedin_url
                company_country = company.country

        if deal.primary_contact_id:
            contact = await service.get_contact_by_id(deal.primary_contact_id)
            if contact:
                phone_number = _clean_string(getattr(contact, "phone", None)) or _clean_string(
                    getattr(contact, "mobile_phone", None)
                )
                object.__setattr__(deal, "primary_contact_phone_number", phone_number)

        # Check for active deal-level jobs (for SSE reconnection)
        active_deal_jobs = None
        try:
            active_jobs_data = await deal_job_service.get_active_deal_jobs_status(deal_id)
            if active_jobs_data:
                active_deal_jobs = DealSummaryJobsStatus.model_validate(active_jobs_data)
                logger.debug(
                    "Active deal jobs found for deal %s: %s",
                    deal_id,
                    active_jobs_data,
                )
        except Exception as jobs_err:
            logger.warning(
                "Could not fetch active deal jobs for %s: %s", deal_id, jobs_err, exc_info=True
            )

        # Keep `deal_summary_data` for UI-only detail caching. Reconnect uses the
        # active job payload and should not derive state from this historical blob.
        deal_summary_data = getattr(deal, "deal_summary_data", None)

        # Determine if deal is from internal CRM
        is_internal = False
        if deal.integration_id:
            integration_types_map = await _get_integration_types_map(
                service.session, service.organisation_id, {deal.integration_id}
            )
            is_internal = integration_types_map.get(deal.integration_id, False)

        schema_service: DealSchemaService | None = None
        schema_session = getattr(service, "session", None)
        if is_internal and schema_session is not None:
            schema_service = DealSchemaService(schema_session, service.organisation_id)

        custom_field_keys: list[str] | None = None
        if schema_service is not None and include_empty_custom_columns:
            try:
                custom_field_keys = await schema_service.active_internal_custom_field_keys()
            except Exception as exc:
                logger.warning(
                    "Failed to load custom field definitions for deal %s: %s",
                    deal_id,
                    exc,
                    exc_info=True,
                )

        custom_column_definitions: list[DealCustomColumnDefinition] | None = None
        if schema_service is not None:
            try:
                # Deal detail keeps this compatibility payload for existing
                # controls, but the projection is now backed by DealSchemaService.
                custom_column_definitions = (
                    await schema_service.deal_detail_custom_column_definitions(
                        deal_currency_code=_normalize_currency_code(deal.currency),
                    )
                )
            except Exception as exc:
                # Metadata enrichment is best-effort and should not block core
                # deal detail responses.
                logger.warning(
                    "Failed to load custom field metadata for deal %s: %s",
                    deal_id,
                    exc,
                    exc_info=True,
                )

        custom_columns = _extract_custom_columns(
            getattr(deal, "properties", None),
            is_internal=is_internal,
            known_keys=custom_field_keys,
            include_empty=include_empty_custom_columns,
        )

        # Build streamlined response
        detail = DealDetail(
            deal_id=deal.deal_id,
            name=deal.name,
            amount=deal.amount,
            currency=_normalize_currency_code(deal.currency),
            currency_symbol=_currency_symbol_for(deal.currency),
            predicted_amount=deal.predicted_amount,
            deal_status=_normalize_deal_status(deal.deal_status),
            probability=deal.probability,
            forecast_category=deal.forecast_category,
            stage_id=deal.stage_id,
            stage_name=deal.stage_name,
            stage_label=deal.stage_label,
            pipeline_id=deal.pipeline_id,
            pipeline_name=deal.pipeline_name,
            pipeline_label=deal.pipeline_label,
            source_created_at=deal.source_created_at,
            close_date=deal.close_date,
            expected_close_date=deal.expected_close_date,
            closed_won_at=deal.closed_won_at,
            closed_lost_at=deal.closed_lost_at,
            last_activity_at=deal.last_activity_at,
            last_activity_source=(deal.properties or {}).get(LAST_ACTIVITY_SOURCE_PROPERTY),
            stage_entered_at=deal.stage_entered_at,
            stage_updated_at=deal.stage_updated_at,
            deal_age_days=deal_age_days,
            owner_details=owner_details,
            deal_team=deal_team,
            company_id=deal.company_id,
            company_name=deal.company_name,
            company_domain=deal.company_domain,
            company_industry=company_industry,
            company_website=company_website,
            company_linkedin_url=company_linkedin_url,
            company_country=company_country,
            primary_contact_id=deal.primary_contact_id,
            primary_contact_name=deal.primary_contact_name,
            primary_contact_email=deal.primary_contact_email,
            primary_contact_details=_primary_contact_details_from_deal(deal),
            deal_type=deal.deal_type,
            priority=deal.priority,
            lead_source=deal.lead_source,
            closed_reason=deal.closed_reason,
            crm_url=deal.crm_url,
            description=deal.description,
            deal_health=deal_health,
            deal_health_reason=deal_health_reason,
            is_internal=is_internal,
            custom_columns=custom_columns,
            custom_column_definitions=custom_column_definitions,
            time_tracked=None,  # Placeholder for future implementation
            source_modified_at=deal.source_modified_at,
            last_seen_at=deal.last_seen_at,
            created_at=deal.created_at,
            updated_at=deal.updated_at,
            synced_at=deal.synced_at,
            associated_contact_count=deal.associated_contact_count,
            associated_company_count=deal.associated_company_count,
            note_count=deal.note_count,
            deal_summary_data=deal_summary_data,
            active_deal_jobs=active_deal_jobs,
        )

        return Response.success_response(message="Deal retrieved successfully", data=detail)

    except DealReadServiceError as e:
        logger.warning("Deal access error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
    except Exception as e:
        logger.error("Unexpected error getting deal %s: %s", deal_id, e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.post(
    "/deals",
    response_model=GenericResponse[DealWriteResponse],
    status_code=201,
    tags=["crm", "deals"],
    summary="Create an internal CRM deal",
)
async def create_deal(
    request: DealCreateRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Create a new internal CRM deal."""
    try:
        create_payload = request.model_dump()
        # TODO(copilotgtm): Move web create clients to GET /users/crm/schema?entity=deal
        # before POST /deals so they can render required fields/options before submit.
        # Contact stakeholders need the route-level job/research follow-up below,
        # so keep creation in one place instead of pre-creating participants here.
        create_payload.pop("contact_participants", None)
        deal = await service.create_deal(create_payload)
        inline_stakeholder_ids: list[UUID] = []
        inline_contact_ids: list[UUID] = []
        if request.contact_participants:
            stakeholder_service = StakeholderWriteService(
                service.session,
                service.organisation_id,
                service.actor_user_id,
            )
            stakeholder_result = await stakeholder_service.add_contacts_as_stakeholders(
                deal.deal_id,
                [participant.contact_id for participant in request.contact_participants],
            )
            inline_stakeholder_ids = [
                stakeholder.participant_id for stakeholder in stakeholder_result.added
            ]
            inline_contact_ids = [
                stakeholder.contact_id
                for stakeholder in stakeholder_result.added
                if stakeholder.contact_id
            ]
        await service.session.commit()
        await service.session.refresh(deal)

        processing_jobs = []
        job_service = DealJobService(
            service.session,
            service.organisation_id,
            service.actor_user_id,
        )
        if inline_stakeholder_ids:
            try:
                processing_jobs = await job_service.create_stakeholder_processing_jobs(
                    deal.deal_id,
                    participant_ids=inline_stakeholder_ids,
                )
            except Exception:
                await service.session.rollback()
                logger.error(
                    "Failed to create stakeholder processing jobs after deal creation",
                    extra={
                        "organisation_id": str(service.organisation_id),
                        "deal_id": str(deal.deal_id),
                        "participant_ids": [str(pid) for pid in inline_stakeholder_ids],
                    },
                    exc_info=True,
                )
            for contact_id in inline_contact_ids:
                await _contact_research_job_response(
                    organisation_id=service.organisation_id,
                    contact_id=contact_id,
                    user_id=service.actor_user_id,
                )

        if (
            request.contact_participants or request.primary_contact_email
        ) and not inline_stakeholder_ids:
            try:
                processing_jobs = [
                    await job_service.create_communication_hydration_job(
                        deal.deal_id,
                    )
                ]
            except Exception:
                logger.warning(
                    "Failed to replay communications after deal creation",
                    extra={"deal_id": str(deal.deal_id)},
                    exc_info=True,
                )

        payload = _map_deal_write_response(deal)
        payload.processing_jobs = [
            DealJobResponse(
                job_id=job.job_id,
                job_type=job.job_type,
                status=job.status,
            )
            for job in processing_jobs
        ]
        return Response.created_response(
            message="Deal created successfully", data=payload, response=response
        )
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal create error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error creating deal: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.patch(
    "/deals/{deal_id}",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Update an internal CRM deal",
)
async def update_deal(
    deal_id: UUID,
    request: DealUpdateRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Update fields for an internal CRM deal."""
    try:
        deal = await service.update_deal(deal_id, request.model_dump(exclude_unset=True))
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
        return Response.success_response(message="Deal updated successfully", data=payload)
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error updating deal: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.patch(
    "/deals/{deal_id}/account",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Update account details for an internal CRM deal",
)
async def update_deal_account(
    deal_id: UUID,
    request: DealAccountUpdateRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Update account details for an internal CRM deal."""
    try:
        deal = await service.update_account(deal_id, request.model_dump(exclude_unset=True))
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
        return Response.success_response(message="Deal account updated successfully", data=payload)
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal account update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error updating deal account: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.patch(
    "/deals/{deal_id}/primary-contact",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Update primary contact details for an internal CRM deal",
)
async def update_deal_primary_contact(
    deal_id: UUID,
    request: DealPrimaryContactUpdateRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Update primary contact details for an internal CRM deal."""
    try:
        deal = await service.update_primary_contact(
            deal_id=deal_id,
            primary_contact_id=request.primary_contact_id,
            primary_contact_name=request.primary_contact_name,
            primary_contact_email=request.primary_contact_email,
            clear_primary_contact=request.is_clear_request,
        )
        primary_contact_details = _primary_contact_details_from_deal(deal)
        await service.session.commit()
        await service.session.refresh(deal)
        processing_jobs = []
        try:
            processing_jobs = [
                await DealJobService(
                    service.session,
                    service.organisation_id,
                    service.actor_user_id,
                ).create_communication_hydration_job(
                    deal.deal_id,
                    trigger="deal_primary_contact_changed",
                )
            ]
        except Exception:
            logger.warning(
                "Failed to replay communications after primary contact update",
                extra={"deal_id": str(deal.deal_id)},
                exc_info=True,
            )
        payload = _map_deal_write_response(
            deal,
            primary_contact_details=primary_contact_details,
        )
        payload.processing_jobs = [
            DealJobResponse(
                job_id=job.job_id,
                job_type=job.job_type,
                status=job.status,
            )
            for job in processing_jobs
        ]
        return Response.success_response(
            message="Deal primary contact updated successfully",
            data=payload,
        )
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal primary contact update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error updating deal primary contact: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


@deals_router.delete(
    "/deals/{deal_id}",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Delete an internal CRM deal",
)
async def delete_deal(
    deal_id: UUID,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:delete")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Soft delete an internal CRM deal."""
    try:
        deal = await service.delete_deal(deal_id)
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
        return Response.success_response(message="Deal deleted successfully", data=payload)
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal delete error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error deleting deal: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.post(
    "/deals/{deal_id}/stage",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Move an internal CRM deal to a new stage",
)
async def move_deal_stage(
    deal_id: UUID,
    request: DealStageMoveRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Move a deal to a new stage and record stage history."""
    try:
        deal = await service.move_stage(deal_id, request.stage_id)
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
        return Response.success_response(message="Deal stage updated successfully", data=payload)
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal stage move error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error moving deal stage: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.post(
    "/deals/{deal_id}/close",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Close an internal CRM deal as won or lost",
)
async def close_deal(
    deal_id: UUID,
    request: DealCloseRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Close an internal CRM deal and capture close metadata."""
    try:
        deal = await service.close_deal(
            deal_id=deal_id,
            outcome=request.outcome,
            closed_reason=request.closed_reason,
            close_date=request.close_date,
            stage_id=request.stage_id,
        )
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
        return Response.success_response(message="Deal closed successfully", data=payload)
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal close error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error closing deal: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.post(
    "/deals/{deal_id}/reopen",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Reopen an internal CRM deal",
)
async def reopen_deal(
    deal_id: UUID,
    request: DealReopenRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Reopen an internal CRM deal and move it back to an open stage."""
    try:
        deal = await service.reopen_deal(
            deal_id=deal_id,
            stage_id=request.stage_id,
            clear_closed_reason=request.clear_closed_reason,
        )
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
        return Response.success_response(message="Deal reopened successfully", data=payload)
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal reopen error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error reopening deal: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.post(
    "/deals/{deal_id}/owner",
    response_model=GenericResponse[DealWriteResponse],
    tags=["crm", "deals"],
    summary="Assign an owner to an internal CRM deal",
)
async def assign_deal_owner(
    deal_id: UUID,
    request: DealOwnerAssignRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deals:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Assign or clear the owner for an internal CRM deal."""
    try:
        deal = await service.assign_owner(deal_id, request.owner_user_id)
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
        return Response.success_response(message="Deal owner updated successfully", data=payload)
    except DealWriteServiceError as exc:
        await service.session.rollback()
        logger.warning("Deal owner assign error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error assigning deal owner: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deals_router.post(
    "/deals/{deal_id}/deal-summary/generate",
    response_model=GenericResponse[DealJobResponse],
    tags=["crm", "deals", "ai"],
    summary="Generate AI summary for a deal",
    dependencies=[Depends(require_deal_collaborate)],
    description="""
    Initiate an AI-powered summary generation for a deal.

    This analyzes ALL meeting transcripts associated with the deal to produce
    a comprehensive deal summary including:
    - Overall deal narrative and progression
    - Key themes across meetings
    - Relationship progression
    - Critical decisions made
    - Outstanding items and risks
    - Recommended next steps

    If a summary job is already running, returns the existing job_id (idempotent).
    Use the returned job_id to subscribe to the SSE stream for progress updates.
    """,
)
async def generate_deal_summary(
    deal_id: UUID,
    response: FastAPIResponse,
    service: DealJobService = Depends(get_deal_job_service),
    _crm_write_guard: None = Depends(require_deals_update_scope),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealJobResponse]:
    """
    Generate an AI-powered summary for a deal.

    Analyzes all meeting transcripts to produce a comprehensive deal summary.
    """
    try:
        job = await service.create_deal_summary_job(deal_id)

        # Determine if this is an existing job or a new one
        is_new = job.status == JobStatus.PENDING
        message = (
            "Deal summary generation initiated"
            if is_new
            else "Deal summary generation already in progress"
        )

        return Response.accepted_response(
            message,
            DealJobResponse(
                job_id=job.job_id,
                job_type=str(job.job_type),
                status=str(job.status),
            ),
            response=response,
        )
    except DealJobServiceError as e:
        logger.warning("Deal summary job error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            response=response,
        )
    except Exception:
        logger.exception("Failed to initiate deal summary for deal %s", deal_id)
        return Response.error_response(
            message="Failed to initiate deal summary",
            status_code=500,
            response=response,
        )


@deals_router.get(
    "/deals/{deal_id}/jobs/{job_id}/stream",
    tags=["crm", "deals", "jobs"],
    summary="SSE stream for a deal job",
    dependencies=[Depends(require_deal_view)],
    description="""
    Generic SSE stream for any user-visible deal job.

    Subscribe with the job_id returned by a deal or stakeholder mutation. The stream
    emits progress frames from Redis and supports reconnect through Last-Event-ID.
    """,
)
async def deal_job_stream(
    deal_id: UUID,
    job_id: UUID,
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
):
    """SSE stream for any user-visible deal job."""
    sse_service = DealJobSSEService(
        user_ids.organisation_id,
        job_id,
        user_ids.user_id,
    )
    can_access = await sse_service.ensure_deal_access(
        deal_id,
        event_id=None,
        allowed_job_types=GENERIC_DEAL_STREAM_JOB_TYPES,
    )
    if can_access is None:
        raise HTTPException(status_code=404, detail="Job stream not found")
    if can_access is False:
        raise HTTPException(status_code=403, detail="Not authorized to access this job stream")

    logger.info(
        "Client connected to generic deal job SSE: org=%s deal=%s job=%s",
        user_ids.organisation_id,
        deal_id,
        job_id,
    )

    return _job_stream_response(sse_service, request)


@deals_router.get(
    "/jobs/{job_id}/stream",
    tags=["crm", "jobs"],
    summary="SSE stream for a user-owned CRM job",
)
async def user_job_stream(
    job_id: UUID,
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
):
    """Stream a contact-scoped job after enforcing tenant and user ownership."""
    sse_service = DealJobSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    # Contact research has no deal_id. User ownership remains the authorization
    # boundary, while the org-scoped session prevents cross-tenant job lookup.
    can_access = await sse_service.ensure_user_access()
    if can_access is None:
        raise HTTPException(status_code=404, detail="Job stream not found")
    if can_access is False:
        raise HTTPException(status_code=403, detail="Not authorized to access this job stream")

    logger.info(
        "Client connected to user-owned CRM job SSE: org=%s job=%s",
        user_ids.organisation_id,
        job_id,
    )
    return _job_stream_response(sse_service, request)


@deals_router.get(
    "/deals/{deal_id}/deal-summary/stream/{job_id}",
    tags=["crm", "deals", "ai"],
    summary="SSE stream for deal summary job",
    dependencies=[Depends(require_deal_view)],
    description="""
    SSE (Server-Sent Events) stream for deal summary job progress and results.

    Subscribe to this endpoint with the job_id from POST /deals/{deal_id}/deal-summary/generate.
    The stream will emit progress events during generation and the final result when complete.

    Handles reconnection - if user disconnects and reconnects, the stream resumes from the
    current state or delivers the cached result if already completed.
    """,
)
async def deal_summary_stream(
    deal_id: UUID,
    job_id: UUID,
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
):
    """
    SSE stream for deal summary job progress and results.

    Subscribe with the job_id from POST /deals/{deal_id}/deal-summary/generate.
    """
    sse_service = DealJobSSEService(
        user_ids.organisation_id,
        job_id,
        user_ids.user_id,
    )
    can_access = await sse_service.ensure_deal_access(
        deal_id,
        event_id=None,
        allowed_job_types=DEAL_SUMMARY_STREAM_JOB_TYPES,
    )
    if can_access is None:
        raise HTTPException(status_code=404, detail="Job stream not found")
    if can_access is False:
        raise HTTPException(status_code=403, detail="Not authorized to access this job stream")

    last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
    resume_from_id = last_event_id or None

    logger.info(
        "Client connected to deal summary SSE: org=%s deal=%s job=%s",
        user_ids.organisation_id,
        deal_id,
        job_id,
    )

    async def event_generator():
        """Stream frames from summary SSE while ensuring stream cleanup on disconnect."""
        try:
            async for ev in sse_service.stream_events(resume_from_id=resume_from_id):
                yield ev
        finally:
            try:
                await sse_service.close()
            except Exception:
                logger.debug("DealJobSSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())
