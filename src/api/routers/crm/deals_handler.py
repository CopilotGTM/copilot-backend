"""CRM Deals API handler - tenant-scoped endpoints for accessing deal data."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_models import (
    DealAssociationsContext,
    DealContext,
    DealDetail,
    DealFinancialContext,
    DealPipelineContext,
    DealsPage,
    DealsStats,
    DealTimelineContext,
    PipelineStage,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.infra.utils.logger import get_logger
from src.service.crm.deals_access_service import (
    DealsAccessService,
    DealsAccessServiceError,
)
from src.workers.deal_worker.tasks import process_deal_meetings_task

logger = get_logger("deals_handler")

# Common error message (Sonar S1192 compliance)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deals_router = APIRouter()


async def _build_deal_context(
    deal, deals_service: DealsAccessService, stage_history: list | None = None
) -> DealContext:
    """Assemble the composite context block for a deal response."""

    # Calculate Deal Age
    deal_age_days = None
    if getattr(deal, "source_created_at", None):
        created_at = deal.source_created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        deal_age_days = (datetime.now(UTC) - created_at).days

    timeline = DealTimelineContext(
        source_created_at=getattr(deal, "source_created_at", None),
        close_date=getattr(deal, "close_date", None),
        expected_close_date=getattr(deal, "expected_close_date", None),
        closed_won_at=getattr(deal, "closed_won_at", None),
        closed_lost_at=getattr(deal, "closed_lost_at", None),
        last_activity_at=getattr(deal, "last_activity_at", None),
        stage_entered_at=getattr(deal, "stage_entered_at", None),
        stage_updated_at=getattr(deal, "stage_updated_at", None),
        synced_at=getattr(deal, "synced_at", None),
        source_modified_at=getattr(deal, "source_modified_at", None),
        last_seen_at=getattr(deal, "last_seen_at", None),
        days_to_close=getattr(deal, "days_to_close", None),
        deal_age_days=deal_age_days,
    )

    financials = DealFinancialContext(
        amount=getattr(deal, "amount", None),
        currency=getattr(deal, "currency", None),
        amount_home_currency=getattr(deal, "amount_home_currency", None),
        predicted_amount=getattr(deal, "predicted_amount", None),
        probability=getattr(deal, "probability", None),
    )

    # Try to find technical owner in participants first
    tech_owner_participant = next(
        (
            p
            for p in getattr(deal, "participants", [])
            if getattr(p, "relationship_role", "") == "technical_owner"
        ),
        None,
    )

    if tech_owner_participant:
        tech_owner_id = getattr(tech_owner_participant, "related_external_id", None)
        tech_owner_name = getattr(tech_owner_participant, "related_name", None)
        tech_owner_email = getattr(tech_owner_participant, "related_email", None)
    else:
        tech_owner_id = None
        tech_owner_name = None
        tech_owner_email = None

    # Fetch Company Industry
    company_industry = None
    company_external_id = getattr(deal, "company_external_id", None)
    if company_external_id:
        company = await deals_service.get_company_by_external_id(company_external_id)
        if company:
            company_industry = company.industry

    associations = DealAssociationsContext(
        owner_external_id=getattr(deal, "owner_external_id", None),
        owner_name=getattr(deal, "owner_name", None),
        owner_email=getattr(deal, "owner_email", None),
        technical_owner_external_id=tech_owner_id,
        technical_owner_name=tech_owner_name,
        technical_owner_email=tech_owner_email,
        company_external_id=company_external_id,
        company_name=getattr(deal, "company_name", None),
        company_domain=getattr(deal, "company_domain", None),
        company_industry=company_industry,
        primary_contact_external_id=getattr(deal, "primary_contact_external_id", None),
        primary_contact_name=getattr(deal, "primary_contact_name", None),
        primary_contact_email=getattr(deal, "primary_contact_email", None),
    )

    # Calculate Health
    health = "HEALTHY"
    deal_status = getattr(deal, "deal_status", "open")
    if deal_status in ["won", "lost", "closed"]:
        health = deal_status.upper()
    elif not getattr(deal, "last_activity_at", None):
        health = "STALLED"
    else:
        last_activity = deal.last_activity_at
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=UTC)
        days_since_activity = (datetime.now(UTC) - last_activity).days
        if days_since_activity > 14:
            health = "STALLED"

    # Time Tracked (Mocked for now as per image requirement)
    time_tracked = "47 hours"

    # Get pipeline info
    pipeline_name = "Default Pipeline"
    pipeline_id = None
    all_stages = []

    if deal.stage and deal.stage.pipeline:
        pipeline_name = deal.stage.pipeline.name
        pipeline_id = deal.stage.pipeline.pipeline_id

        # Fetch all stages for this pipeline
        stages_db = await deals_service.get_pipeline_stages(pipeline_id)

        # Map history by stage_external_id (or stage_id if available in history)
        # stage_history is a list of dicts
        history_map = {}
        if stage_history:
            for h in stage_history:
                # Prefer matching by external_id as it's more robust across syncs
                # But we need to check what keys are in h.
                # Based on get_deal_stage_history: "stage_external_id", "stage_name", etc.
                eid = h.get("stage_external_id")
                if eid:
                    history_map[eid] = h

        current_stage_order = deal.stage.display_order or 0

        all_stages = []
        for s in stages_db:
            # Determine status and history
            status = "upcoming"
            entered_at = None
            exited_at = None
            duration_days = None

            # Check history
            hist_item = history_map.get(s.external_id)
            if hist_item:
                entered_at = hist_item.get("entered_at")
                exited_at = hist_item.get("exited_at")
                duration_days = hist_item.get("duration_days")

                if s.stage_id == deal.stage_id:
                    status = "active"
                elif exited_at:
                    status = "completed"
                else:
                    # In history but not active and no exit time?
                    # Could be a previous visit that wasn't closed properly, or current.
                    # If it matches current ID, it's active.
                    status = "completed"  # Fallback
            else:
                # No history record
                if s.stage_id == deal.stage_id:
                    status = "active"
                elif (s.display_order or 0) < current_stage_order:
                    status = "skipped"
                else:
                    status = "upcoming"

            all_stages.append(
                PipelineStage(
                    stage_id=s.stage_id,
                    external_id=s.external_id or str(s.stage_id),
                    name=s.name,
                    label=s.label,
                    display_order=s.display_order or 0,
                    probability=s.probability,
                    is_closed=s.is_closed or False,
                    status=status,
                    entered_at=entered_at,
                    exited_at=exited_at,
                    duration_days=duration_days,
                )
            )

    pipeline_context = DealPipelineContext(
        pipeline_id=pipeline_id,
        pipeline_external_id=getattr(deal, "pipeline_external_id", None),
        pipeline_name=pipeline_name,
        pipeline_label=getattr(deal, "pipeline_label", None),
        stage_id=deal.stage_id,
        stage_external_id=getattr(deal, "stage_external_id", None),
        stage_name=deal.stage.name if deal.stage else None,
        stage_label=getattr(deal, "stage_label", None),
        forecast_category=getattr(deal, "forecast_category", None),
        deal_type=getattr(deal, "deal_type", None),
        priority=getattr(deal, "priority", None),
        lead_source=getattr(deal, "lead_source", None),
        stages=all_stages,
    )

    context = DealContext(
        pipeline=pipeline_context,
        timeline=timeline,
        financials=financials,
        associations=associations,
        stage_history=stage_history or [],
        metrics=getattr(deal, "metrics", None) or None,
        properties=getattr(deal, "properties", None) or None,
        health=health,
        time_tracked=time_tracked,
    )
    return context


async def get_deals_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealsAccessService instance for the current org.

    Args:
        user_ids: Current user and organisation IDs from JWT
        org_session: Async database session scoped to the organisation

    Yields:
        DealsAccessService instance
    """
    service = DealsAccessService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Deals service close failed", exc_info=True)


@deals_router.get(
    "/deals",
    response_model=GenericResponse[DealsPage],
    tags=["crm", "deals"],
    summary="List CRM deals with pagination and filtering",
    description="""
    Retrieve a paginated list of CRM deals for the current organisation.

    Supports filtering by:
    - Deal status (OPEN, WON, LOST, CLOSED)
    - Pipeline ID
    - Owner external ID
    - Deal Amount range (min/max)
    - Close Date range (min/max)
    - Search query on deal name (case-insensitive)

    Uses cursor-based pagination for efficient traversal of large datasets.
    """,
)
async def list_deals(
    limit: int = Query(20, ge=1, le=100, description="Maximum deals per page"),
    cursor: str | None = Query(
        None, description="Opaque cursor from previous page (omit for first page)"
    ),
    statuses: list[CRMDealStatus] | None = Query(None, description="Filter by deal statuses"),
    pipeline_ids: list[UUID] | None = Query(None, description="Filter by pipeline IDs"),
    owner_external_ids: list[str] | None = Query(None, description="Filter by owner external IDs"),
    min_amount: float | None = Query(None, description="Minimum deal amount"),
    max_amount: float | None = Query(None, description="Maximum deal amount"),
    min_close_date: datetime | None = Query(None, description="Minimum close date"),
    max_close_date: datetime | None = Query(None, description="Maximum close date"),
    q: str | None = Query(None, description="Case-insensitive search on deal name"),
    service: DealsAccessService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    """List CRM deals with optional filters and pagination.

    Returns:
        GenericResponse containing DealsPage with deals list and next cursor
    """
    try:
        deals, next_cursor = await service.list_deals(
            limit=limit,
            cursor=cursor,
            statuses=statuses,
            pipeline_ids=pipeline_ids,
            owner_external_ids=owner_external_ids,
            min_amount=min_amount,
            max_amount=max_amount,
            min_close_date=min_close_date,
            max_close_date=max_close_date,
            q=q,
        )

        # Convert to response models
        items = [
            {
                "deal_id": deal.deal_id,
                "external_id": deal.external_id,
                "name": deal.name,
                "amount": deal.amount,
                "currency": deal.currency,
                "deal_status": deal.deal_status,
                "probability": deal.probability,
                "close_date": deal.close_date,
                "stage_name": deal.stage_name,
                "stage_label": deal.stage_label,
                "pipeline_name": deal.pipeline_name,
                "pipeline_label": deal.pipeline_label,
                "owner_name": deal.owner_name,
                "owner_email": deal.owner_email,
                "company_external_id": deal.company_external_id,
                "company_name": getattr(deal, "company_name", None),
                "company_domain": getattr(deal, "company_domain", None),
                "primary_contact_external_id": deal.primary_contact_external_id,
                "primary_contact_name": getattr(deal, "primary_contact_name", None),
                "primary_contact_email": getattr(deal, "primary_contact_email", None),
                "created_at": deal.created_at,
                "updated_at": deal.updated_at,
                "synced_at": deal.synced_at,
            }
            for deal in deals
        ]

        page = DealsPage(items=items, cursor=next_cursor)
        return Response.success_response(message="Deals retrieved successfully", data=page)

    except DealsAccessServiceError as e:
        logger.warning("Deals access error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
        )
    except Exception as e:
        logger.error("Unexpected error listing deals: %s", e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
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
    service: DealsAccessService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
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
        )
    except Exception as e:
        logger.error("Unexpected error calculating stats: %s", e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
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
    """,
)
async def get_deal(
    deal_id: UUID,
    service: DealsAccessService = Depends(get_deals_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    """Retrieve detailed information for a single deal.

    Args:
        deal_id: UUID of the deal to retrieve

    Returns:
        GenericResponse containing DealDetail with full deal information
    """
    try:
        deal = await service.get_deal_by_id(deal_id)

        if not deal:
            return Response.error_response(
                message="Deal not found",
                status_code=404,
            )

        # Fetch stage history for the deal
        stage_history = await service.get_deal_stage_history(deal_id)

        # Convert to detailed response model
        base_detail = DealDetail.model_validate(deal)
        detail_with_context = base_detail.model_copy(
            update={"context": await _build_deal_context(deal, service, stage_history)}
        )
        return Response.success_response(
            message="Deal retrieved successfully", data=detail_with_context
        )

    except DealsAccessServiceError as e:
        logger.warning("Deal access error: %s", e.message, exc_info=True)
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
        )
    except Exception as e:
        logger.error("Unexpected error getting deal %s: %s", deal_id, e, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
        )


@deals_router.post(
    "/process-meetings",
    response_model=GenericResponse[dict],
    summary="Trigger deal meeting processing",
    description="Manually trigger the background task to identify meetings associated with deals and create tasks.",
)
async def trigger_deal_meeting_processing(
    user: UserIDs = Depends(get_current_user),
) -> GenericResponse[dict]:
    """Trigger the deal meeting processing task."""
    try:
        task = process_deal_meetings_task.delay(str(user.organisation_id))
        return Response.success_response(
            message="Deal meeting processing triggered successfully",
            data={"task_id": task.id},
        )
    except Exception as e:
        logger.error(f"Failed to trigger deal meeting processing: {e}", exc_info=True)
        return Response.error_response(
            message="Failed to trigger processing",
            status_code=500,
        )
