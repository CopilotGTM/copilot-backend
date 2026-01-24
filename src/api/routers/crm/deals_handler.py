"""CRM Deals API handler - tenant-scoped endpoints for accessing deal data."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_deal_write_models import (
    DealAccountUpdateRequest,
    DealCreateRequest,
    DealOwnerAssignRequest,
    DealStageMoveRequest,
    DealUpdateRequest,
    DealWriteResponse,
)
from src.api.schemas.crm_models import (
    DealDetail,
    DealJobResponse,
    DealListItem,
    DealOwnerDetail,
    DealOwnerFilterItem,
    DealOwnerFilters,
    DealsListParams,
    DealsPage,
    DealsStats,
    DealSummaryJobsStatus,
    DealTeamMember,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.job_models import JobStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.deals_access_service import (
    DealsAccessService,
    DealsAccessServiceError,
    extract_deal_health,
    extract_deal_health_reason,
)
from src.service.crm.internal.deal_write_service import DealWriteService, DealWriteServiceError
from src.service.crm.jobs.deal_job_service import DealJobService, DealJobServiceError
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService
from sse_starlette.sse import EventSourceResponse

logger = get_logger("deals_handler")

# Common error message (Sonar S1192 compliance)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deals_router = APIRouter()


def get_deals_list_params(
    limit: int = Query(40, ge=1, le=100, description="Maximum deals per page"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    statuses: list[CRMDealStatus] | None = Query(None, description="Filter by deal statuses"),
    pipeline_ids: list[UUID] | None = Query(None, description="Filter by pipeline IDs"),
    owner_ids: list[UUID] | None = Query(None, description="Filter by internal owner IDs"),
    owner_external_ids: list[str] | None = Query(
        None, description="Deprecated. Filter by owner external IDs"
    ),
    min_amount: float | None = Query(None, description="Minimum deal amount"),
    max_amount: float | None = Query(None, description="Maximum deal amount"),
    min_close_date: datetime | None = Query(None, description="Minimum close date"),
    max_close_date: datetime | None = Query(None, description="Maximum close date"),
    q: str | None = Query(None, description="Case-insensitive search on deal name"),
) -> DealsListParams:
    """Dependency that parses and returns deals list query parameters."""
    return DealsListParams(
        limit=limit,
        page=page,
        statuses=statuses,
        pipeline_ids=pipeline_ids,
        owner_ids=owner_ids,
        owner_external_ids=owner_external_ids,
        min_amount=min_amount,
        max_amount=max_amount,
        min_close_date=min_close_date,
        max_close_date=max_close_date,
        q=q,
    )


async def get_deals_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealsAccessService instance for the current org."""
    service = DealsAccessService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Deals service close failed", exc_info=True)


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
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealWriteService close failed", exc_info=True)


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

    # Create mapping of integration_id -> is_internal
    return {
        integration.integration_id: integration.integration_type == IntegrationType.INTERNAL_CRM
        for integration in integrations
    }


def _map_deal_write_response(deal) -> DealWriteResponse:
    return DealWriteResponse(
        deal_id=deal.deal_id,
        external_id=deal.external_id,
        name=deal.name,
        pipeline_id=deal.pipeline_id,
        stage_id=deal.stage_id,
        deal_status=str(deal.deal_status),
        owner_user_id=deal.owner_id,
        company_external_id=getattr(deal, "company_external_id", None),
        company_name=getattr(deal, "company_name", None),
        company_domain=getattr(deal, "company_domain", None),
        created_at=deal.created_at,
        updated_at=deal.updated_at,
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
    - Close Date range (min/max)
    - Search query on deal name (case-insensitive)
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
    service: DealsAccessService = Depends(get_deals_access_service),
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
            owner_external_ids=params.owner_external_ids,
            min_amount=params.min_amount,
            max_amount=params.max_amount,
            min_close_date=params.min_close_date,
            max_close_date=params.max_close_date,
            q=params.q,
        )

        # Fetch deal team members (owners, collaborators, split_owners) in one batch query
        deal_ids = [deal.deal_id for deal in deals]
        team_by_deal = await service.get_deal_team_members_batch(deal_ids) if deal_ids else {}

        # Batch fetch integration types to determine is_internal
        integration_ids = {deal.integration_id for deal in deals if deal.integration_id}
        integration_types_map = await _get_integration_types_map(
            service.session, service.organisation_id, integration_ids
        )

        # Convert to response models
        items: list[DealListItem] = [
            DealListItem(
                deal_id=deal.deal_id,
                external_id=deal.external_id,
                name=deal.name,
                amount=deal.amount,
                currency=deal.currency,
                deal_status=deal.deal_status,
                probability=deal.probability,
                close_date=deal.close_date,
                stage_name=deal.stage_name,
                stage_label=deal.stage_label,
                pipeline_name=deal.pipeline_name,
                pipeline_label=deal.pipeline_label,
                owner_details=(
                    [
                        DealOwnerDetail(
                            external_id=str(deal.owner_external_id),
                            name=deal.owner_name,
                            email=deal.owner_email,
                            owner_id=deal.owner_id,
                        )
                    ]
                    if getattr(deal, "owner_external_id", None)
                    else []
                ),
                company_external_id=deal.company_external_id,
                company_name=getattr(deal, "company_name", None),
                company_domain=getattr(deal, "company_domain", None),
                deal_health=extract_deal_health(getattr(deal, "deal_summary_data", None)),
                deal_health_reason=extract_deal_health_reason(
                    getattr(deal, "deal_summary_data", None)
                ),
                deal_team=[
                    DealTeamMember(
                        external_id=member["external_id"],
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

    except DealsAccessServiceError as e:
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
    limit: int = Query(200, ge=1, le=1000, description="Maximum owners to return"),
    q: str | None = Query(None, description="Search by owner name, email, or external ID"),
    service: DealsAccessService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[DealOwnerFilters]:
    """List distinct deal owners for filter dropdowns."""
    try:
        owners = await service.list_deal_owners(q=q, limit=limit)
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
    except DealsAccessServiceError as e:
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
    service: DealsAccessService = Depends(get_deals_access_service),
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

    except DealsAccessServiceError as e:
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
    - Activity timeline
    - Custom properties and metrics
    - Deal summary data (if generated)
    - Active deal-level jobs (for SSE reconnection)
    """,
)
async def get_deal(
    deal_id: UUID,
    response: FastAPIResponse,
    service: DealsAccessService = Depends(get_deals_access_service),
    deal_job_service: DealJobService = Depends(get_deal_job_service),
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
                    external_id=str(deal.owner_external_id),
                    name=deal.owner_name,
                    email=deal.owner_email,
                    owner_id=deal.owner_id,
                )
            ]
            if getattr(deal, "owner_external_id", None)
            else []
        )

        # Build deal_team list
        deal_team = [
            DealTeamMember(
                external_id=member["external_id"],
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

        # Fetch company industry from associated company (industry is on CRMCompany, not CRMDeal)
        company_industry = None
        if deal.company_external_id:
            company = await service.get_company_by_external_id(deal.company_external_id)
            if company:
                company_industry = company.industry

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

        # Get deal summary data (frontend derives status from active_deal_jobs and deal_summary_data)
        deal_summary_data = getattr(deal, "deal_summary_data", None)

        # Determine if deal is from internal CRM
        is_internal = False
        if deal.integration_id:
            integration_types_map = await _get_integration_types_map(
                service.session, service.organisation_id, {deal.integration_id}
            )
            is_internal = integration_types_map.get(deal.integration_id, False)

        # Build streamlined response
        detail = DealDetail(
            deal_id=deal.deal_id,
            external_id=deal.external_id,
            name=deal.name,
            amount=deal.amount,
            currency=deal.currency,
            deal_status=deal.deal_status,
            stage_name=deal.stage_name,
            stage_label=deal.stage_label,
            source_created_at=deal.source_created_at,
            close_date=deal.close_date,
            last_activity_at=deal.last_activity_at,
            deal_age_days=deal_age_days,
            owner_details=owner_details,
            deal_team=deal_team,
            company_name=deal.company_name,
            company_industry=company_industry,
            deal_health=deal_health,
            deal_health_reason=deal_health_reason,
            is_internal=is_internal,
            time_tracked=None,  # Placeholder for future implementation
            deal_summary_data=deal_summary_data,
            active_deal_jobs=active_deal_jobs,
        )

        return Response.success_response(message="Deal retrieved successfully", data=detail)

    except DealsAccessServiceError as e:
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
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealWriteResponse]:
    """Create a new internal CRM deal."""
    try:
        deal = await service.create_deal(request.model_dump())
        await service.session.commit()
        await service.session.refresh(deal)
        payload = _map_deal_write_response(deal)
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
    description="""
    Initiate an AI-powered summary generation for a deal.

    This analyzes ALL meeting transcripts associated with the deal to produce
    a comprehensive deal summary including:
    - Overall deal narrative and progression
    - Key themes across meetings
    - Relationship timeline
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
    "/deals/{deal_id}/deal-summary/stream/{job_id}",
    tags=["crm", "deals", "ai"],
    summary="SSE stream for deal summary job",
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
    user_ids: UserIDs = Depends(get_current_user),
):
    """
    SSE stream for deal summary job progress and results.

    Subscribe with the job_id from POST /deals/{deal_id}/deal-summary/generate.
    """
    sse_service = DealJobSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    logger.info(
        "Client connected to deal summary SSE: org=%s deal=%s job=%s",
        user_ids.organisation_id,
        deal_id,
        job_id,
    )

    async def event_generator():
        try:
            async for ev in sse_service.stream_events():
                yield ev
        finally:
            try:
                await sse_service.close()
            except Exception:
                logger.debug("DealJobSSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())
