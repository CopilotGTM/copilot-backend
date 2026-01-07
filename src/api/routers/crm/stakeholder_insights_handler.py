"""Stakeholder Insights API handler for managing AI-generated stakeholder insights."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.stakeholder_insight_models import (
    InsightAction,
    ResolveInsightRequest,
    StakeholderInsightListResponse,
    StakeholderInsightResponse,
    StakeholderInsightSummary,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    DealStakeholderInsight,
    StakeholderInsightPriority,
    StakeholderInsightStatus,
    StakeholderInsightType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.stakeholder_insight_service import (
    StakeholderInsightNotFoundError,
    StakeholderInsightService,
    StakeholderInsightServiceError,
)

logger = get_logger("stakeholder_insights_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

stakeholder_insights_router = APIRouter()


async def get_insight_service(
    user: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a StakeholderInsightService instance for the current user/org."""
    service = StakeholderInsightService(
        session=session,
        organisation_id=user.organisation_id,
        user_id=user.user_id,
        user_name=user.user_name if hasattr(user, "user_name") else None,
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("StakeholderInsightService close failed", exc_info=True)


def _model_to_response(insight: DealStakeholderInsight) -> StakeholderInsightResponse:
    """Convert a DealStakeholderInsight model to API response."""
    return StakeholderInsightResponse(
        insight_id=insight.insight_id,
        insight_type=insight.insight_type,
        priority=insight.priority,
        status=insight.status,
        title=insight.title,
        description=insight.description,
        ai_reasoning=insight.ai_reasoning,
        stakeholder_id=insight.stakeholder_id,
        stakeholder_name=insight.stakeholder_name,
        stakeholder_title=insight.stakeholder_title,
        stakeholder_email=insight.stakeholder_email,
        actions=[
            InsightAction(
                action_type=a.get("action_type", ""),
                label=a.get("label", ""),
                parameters=a.get("parameters", {}),
            )
            for a in insight.actions
        ],
        metric_value=insight.metric_value,
        metric_unit=insight.metric_unit,
        threshold_value=insight.threshold_value,
        trend_direction=insight.trend_direction,
        ai_confidence=insight.ai_confidence,
        source_type=insight.source_type,
        related_risk_blocker_id=insight.related_risk_blocker_id,
        identified_at=insight.identified_at,
        expires_at=insight.expires_at,
        created_at=insight.created_at,
        updated_at=insight.updated_at,
    )


@stakeholder_insights_router.get(
    "/{deal_id}/stakeholder-insights",
    response_model=GenericResponse[StakeholderInsightListResponse],
    summary="List stakeholder insights for a deal",
    description="Get AI-generated stakeholder insights for a deal with filtering options.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def list_stakeholder_insights(
    deal_id: UUID,
    service: StakeholderInsightService = Depends(get_insight_service),
    limit: int = Query(20, ge=1, le=50, description="Number of insights to return"),
    status: str | None = Query(
        None,
        description="Comma-separated statuses to filter (ACTIVE,ACKNOWLEDGED,RESOLVED)",
    ),
    priority: StakeholderInsightPriority | None = Query(None, description="Filter by priority"),
    insight_type: StakeholderInsightType | None = Query(None, description="Filter by insight type"),
    include_expired: bool = Query(False, description="Include expired insights"),
) -> GenericResponse[StakeholderInsightListResponse]:
    """
    Get AI-generated stakeholder insights for a deal.

    Returns insights sorted by priority (CRITICAL > HIGH > MEDIUM > LOW)
    and then by identification time.

    By default, expired/resolved/dismissed insights are excluded.
    Use include_expired=true to see all insights.

    Query parameters:
    - limit: Number of insights to return (1-50, default 20)
    - status: Comma-separated status filter (ACTIVE,ACKNOWLEDGED,RESOLVED,DISMISSED)
    - priority: Filter by priority level (CRITICAL,HIGH,MEDIUM,LOW)
    - insight_type: Filter by insight type
    - include_expired: Include expired insights (default false)
    """
    try:
        # Parse status filter
        parsed_statuses = None
        if status:
            status_strs = [s.strip().upper() for s in status.split(",")]
            parsed_statuses = []
            for s in status_strs:
                try:
                    parsed_statuses.append(StakeholderInsightStatus(s))
                except ValueError:
                    logger.warning(f"Invalid status filter: {s}")

        insights, total_count, summary = await service.list_for_deal(
            deal_id=deal_id,
            limit=limit,
            statuses=parsed_statuses,
            priority=priority,
            insight_type=insight_type,
            include_expired=include_expired,
        )

        return Response.success_response(
            message="Stakeholder insights retrieved successfully",
            data=StakeholderInsightListResponse(
                insights=[_model_to_response(i) for i in insights],
                total_count=total_count,
                summary=StakeholderInsightSummary(**summary),
            ),
        )

    except StakeholderInsightServiceError as e:
        logger.warning(f"Stakeholder insight service error: {e.message}")
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except Exception:
        logger.exception("Failed to list stakeholder insights")
        raise HTTPException(status_code=500, detail=_UNEXPECTED_ERR_MSG) from None


@stakeholder_insights_router.get(
    "/stakeholder-insights/{insight_id}",
    response_model=GenericResponse[StakeholderInsightResponse],
    summary="Get stakeholder insight details",
    description="Get details for a specific stakeholder insight.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_stakeholder_insight(
    insight_id: UUID,
    service: StakeholderInsightService = Depends(get_insight_service),
) -> GenericResponse[StakeholderInsightResponse]:
    """Get details for a specific stakeholder insight."""
    try:
        insight = await service.get_by_id(insight_id)
        if not insight:
            raise StakeholderInsightNotFoundError(insight_id)

        return Response.success_response(
            message="Stakeholder insight retrieved successfully",
            data=_model_to_response(insight),
        )

    except StakeholderInsightServiceError as e:
        logger.warning(f"Stakeholder insight service error: {e.message}")
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except Exception:
        logger.exception("Failed to get stakeholder insight")
        raise HTTPException(status_code=500, detail=_UNEXPECTED_ERR_MSG) from None


@stakeholder_insights_router.post(
    "/{deal_id}/stakeholder-insights/{insight_id}/acknowledge",
    response_model=GenericResponse[StakeholderInsightResponse],
    summary="Acknowledge an insight",
    description="Mark an insight as acknowledged (user saw it).",
    dependencies=[Depends(rate_limit_user_write)],
)
async def acknowledge_insight(
    deal_id: UUID,
    insight_id: UUID,
    service: StakeholderInsightService = Depends(get_insight_service),
) -> GenericResponse[StakeholderInsightResponse]:
    """Mark an insight as acknowledged."""
    try:
        insight = await service.acknowledge(insight_id, deal_id)

        return Response.success_response(
            message="Insight acknowledged successfully",
            data=_model_to_response(insight),
        )

    except StakeholderInsightServiceError as e:
        logger.warning(f"Stakeholder insight service error: {e.message}")
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except Exception:
        logger.exception("Failed to acknowledge insight")
        raise HTTPException(status_code=500, detail=_UNEXPECTED_ERR_MSG) from None


@stakeholder_insights_router.post(
    "/{deal_id}/stakeholder-insights/{insight_id}/dismiss",
    response_model=GenericResponse[StakeholderInsightResponse],
    summary="Dismiss an insight",
    description="Dismiss an insight as not relevant.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def dismiss_insight(
    deal_id: UUID,
    insight_id: UUID,
    service: StakeholderInsightService = Depends(get_insight_service),
) -> GenericResponse[StakeholderInsightResponse]:
    """Dismiss an insight as not relevant."""
    try:
        insight = await service.dismiss(insight_id, deal_id)

        return Response.success_response(
            message="Insight dismissed successfully",
            data=_model_to_response(insight),
        )

    except StakeholderInsightServiceError as e:
        logger.warning(f"Stakeholder insight service error: {e.message}")
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except Exception:
        logger.exception("Failed to dismiss insight")
        raise HTTPException(status_code=500, detail=_UNEXPECTED_ERR_MSG) from None


@stakeholder_insights_router.post(
    "/{deal_id}/stakeholder-insights/{insight_id}/resolve",
    response_model=GenericResponse[StakeholderInsightResponse],
    summary="Resolve an insight",
    description="Mark an insight as resolved (action taken).",
    dependencies=[Depends(rate_limit_user_write)],
)
async def resolve_insight(
    deal_id: UUID,
    insight_id: UUID,
    body: ResolveInsightRequest | None = None,
    service: StakeholderInsightService = Depends(get_insight_service),
) -> GenericResponse[StakeholderInsightResponse]:
    """Mark an insight as resolved."""
    try:
        resolution_notes = body.resolution_notes if body else None
        insight = await service.resolve(insight_id, deal_id, resolution_notes)

        return Response.success_response(
            message="Insight resolved successfully",
            data=_model_to_response(insight),
        )

    except StakeholderInsightServiceError as e:
        logger.warning(f"Stakeholder insight service error: {e.message}")
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except Exception:
        logger.exception("Failed to resolve insight")
        raise HTTPException(status_code=500, detail=_UNEXPECTED_ERR_MSG) from None


__all__ = ["stakeholder_insights_router"]
