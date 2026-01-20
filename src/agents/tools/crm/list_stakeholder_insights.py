"""List stakeholder insights tool for agents.

Tool for listing AI-generated stakeholder insights associated with a deal.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Prevents cross-deal data access (deal-scoped tool)

This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
as parameters - both come from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    DealStakeholderInsight,
    StakeholderInsightPriority,
    StakeholderInsightStatus,
    StakeholderInsightType,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_stakeholder_insights(
    *,
    context: AgentContext,
    limit: int = 20,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    """List AI-generated stakeholder insights for a deal.

    Returns insights about stakeholder engagement, sentiment changes,
    champion opportunities, and other actionable recommendations.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    Args:
        context: AgentContext containing organisation and deal metadata
        limit: Maximum number of insights to return (default: 20)
        include_expired: Whether to include expired/resolved/dismissed insights

    Returns:
        List of insight dictionaries with summary information.
    """
    # SECURITY: Both organisation_id and deal_id come from context, not parameters
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("list_deal_stakeholder_insights: missing organisation_id in context")
        return []

    if not deal_id:
        logger.warning("list_deal_stakeholder_insights: missing deal_id in context")
        return []

    effective_limit = max(1, min(limit, 50))

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    c = cast(Any, DealStakeholderInsight).__table__.c

    # Build query
    # SECURITY: Both organisation_id and deal_id from context (immutable)
    stmt = select(DealStakeholderInsight).where(
        c.organisation_id == organisation_id,
        c.deal_id == deal_id,
    )

    # Filter out expired/resolved/dismissed by default
    if not include_expired:
        active_statuses = [
            StakeholderInsightStatus.ACTIVE.value,
            StakeholderInsightStatus.ACKNOWLEDGED.value,
        ]
        stmt = stmt.where(c.status.in_(active_statuses))

    # Order by identified_at descending (newest first)
    stmt = stmt.order_by(c.identified_at.desc()).limit(effective_limit)

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            insights = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_deal_stakeholder_insights: database error: {e}")
            return []

        # Transform to output format (while session is active)
        output: list[dict[str, Any]] = []
        for insight in insights:
            output.append(
                {
                    "insight_id": str(insight.insight_id),
                    "insight_type": (
                        insight.insight_type.value
                        if isinstance(insight.insight_type, StakeholderInsightType)
                        else insight.insight_type
                    ),
                    "priority": (
                        insight.priority.value
                        if isinstance(insight.priority, StakeholderInsightPriority)
                        else insight.priority
                    ),
                    "status": (
                        insight.status.value
                        if isinstance(insight.status, StakeholderInsightStatus)
                        else insight.status
                    ),
                    "title": insight.title,
                    "description": insight.description,
                    "ai_reasoning": insight.ai_reasoning,
                    "stakeholder_id": str(insight.stakeholder_id)
                    if insight.stakeholder_id
                    else None,
                    "stakeholder_name": insight.stakeholder_name,
                    "stakeholder_title": insight.stakeholder_title,
                    "actions": insight.actions,  # Already a list of dicts
                    "metric_value": insight.metric_value,
                    "metric_unit": insight.metric_unit,
                    "trend_direction": insight.trend_direction,
                    "identified_at": insight.identified_at.isoformat()
                    if insight.identified_at
                    else None,
                }
            )
    # Session released

    logger.info(
        "list_deal_stakeholder_insights: found %d insights for deal %s", len(output), deal_id
    )
    return output


def create_list_deal_stakeholder_insights(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_stakeholder_insights tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
    as parameters - both come from context.
    """

    async def list_deal_stakeholder_insights_tool(
        limit: int = 20,
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        """List AI-generated stakeholder insights for the current deal.

        Retrieves insights about stakeholder engagement gaps, sentiment changes,
        champion opportunities, unaddressed concerns, and other actionable
        recommendations for improving deal outcomes.

        Args:
            limit: Maximum number of insights to return (1-50, default: 20)
            include_expired: Whether to include resolved/dismissed insights

        Note: organisation_id and deal_id are automatically set from context
        and cannot be overridden. This tool only queries the current deal.
        """
        logger.info(
            "[TOOL CALLED] list_deal_stakeholder_insights - limit=%d, include_expired=%s",
            limit,
            include_expired,
        )

        if not context.organisation_id:
            logger.warning(
                "list_deal_stakeholder_insights unavailable: missing organisation_id in context"
            )
            return []

        if not context.deal_id:
            logger.warning("list_deal_stakeholder_insights unavailable: missing deal_id in context")
            return []

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealStakeholderInsights",
            tool_func=list_deal_stakeholder_insights,
            parameters={
                "context": context,
                "limit": limit,
                "include_expired": include_expired,
            },
            description=f"Listing stakeholder insights for deal: {context.deal_id}",
            fallback_value=[],
        )

        insights = list(result) if result else []
        logger.info(
            "[TOOL RESULT] list_deal_stakeholder_insights returned %d insights", len(insights)
        )
        return insights

    return list_deal_stakeholder_insights_tool


__all__ = [
    "list_deal_stakeholder_insights",
    "create_list_deal_stakeholder_insights",
]
