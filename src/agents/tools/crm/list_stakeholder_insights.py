"""List stakeholder insights tool for agents.

Tool for listing AI-generated stakeholder insights associated with a deal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
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
    deal_id: str,
    *,
    context: AgentContext,
    limit: int = 20,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    """List AI-generated stakeholder insights for a deal.

    Returns insights about stakeholder engagement, sentiment changes,
    champion opportunities, and other actionable recommendations.

    Args:
        deal_id: The deal UUID as a string
        context: AgentContext containing session and organisation metadata
        limit: Maximum number of insights to return (default: 20)
        include_expired: Whether to include expired/resolved/dismissed insights

    Returns:
        List of insight dictionaries with summary information.
    """
    session = context.session
    organisation_id = context.organisation_id

    if not session or not organisation_id:
        logger.warning("list_deal_stakeholder_insights: missing session or organisation_id")
        return []

    # Parse deal_id
    try:
        parsed_deal_id = UUID(deal_id)
    except ValueError:
        logger.warning(f"list_deal_stakeholder_insights: invalid deal_id format: {deal_id}")
        return []

    effective_limit = max(1, min(limit, 50))

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    c = cast(Any, DealStakeholderInsight).__table__.c

    # Build query
    stmt = select(DealStakeholderInsight).where(
        c.organisation_id == organisation_id,
        c.deal_id == parsed_deal_id,
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

    try:
        result = await session.execute(stmt)
        insights = result.scalars().all()
    except Exception as e:
        logger.exception(f"list_deal_stakeholder_insights: database error: {e}")
        return []

    # Transform to output format
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
                "stakeholder_id": str(insight.stakeholder_id) if insight.stakeholder_id else None,
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

    logger.info(f"list_deal_stakeholder_insights: found {len(output)} insights for deal {deal_id}")
    return output


def create_list_deal_stakeholder_insights(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_stakeholder_insights tool bound to a context."""

    async def list_deal_stakeholder_insights_tool(
        deal_id: str | None = None,
        limit: int = 20,
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        """List AI-generated stakeholder insights for a deal.

        Retrieves insights about stakeholder engagement gaps, sentiment changes,
        champion opportunities, unaddressed concerns, and other actionable
        recommendations for improving deal outcomes.

        Args:
            deal_id: The deal UUID. Falls back to context.deal_id if not provided.
            limit: Maximum number of insights to return (1-50, default: 20)
            include_expired: Whether to include resolved/dismissed insights

        Returns:
            List of insights with:
            - insight_id: Unique identifier
            - insight_type: ENGAGEMENT_GAP, SENTIMENT_DECLINING, CHAMPION_OPPORTUNITY, etc.
            - priority: CRITICAL, HIGH, MEDIUM, LOW
            - status: ACTIVE, ACKNOWLEDGED, RESOLVED, DISMISSED
            - title: Short summary (e.g., "Decision maker not engaged in 3 weeks")
            - description: Detailed explanation
            - ai_reasoning: Why this insight was generated
            - stakeholder_name/title: Related stakeholder info
            - actions: Suggested actions (SCHEDULE_CALL, SEND_EMAIL, etc.)
            - metric_value/unit: Supporting metrics (e.g., 21 days)
            - identified_at: When the insight was generated
        """
        logger.info(
            f"[TOOL CALLED] list_deal_stakeholder_insights: deal_id={deal_id}, limit={limit}"
        )

        # Fall back to context.deal_id if not provided
        effective_deal_id = deal_id or context.deal_id
        if not effective_deal_id:
            logger.warning("list_deal_stakeholder_insights: no deal_id provided or in context")
            return []

        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="list_deal_stakeholder_insights",
                tool_func=list_deal_stakeholder_insights,
                parameters={
                    "deal_id": effective_deal_id,
                    "context": context,
                    "limit": limit,
                    "include_expired": include_expired,
                },
                description=f"List stakeholder insights for deal {effective_deal_id}",
                fallback_value=[],
            )

        insights = list(result) if result else []
        logger.info(f"[TOOL RESULT] list_deal_stakeholder_insights: {len(insights)} insights")
        return insights

    return list_deal_stakeholder_insights_tool


__all__ = [
    "list_deal_stakeholder_insights",
    "create_list_deal_stakeholder_insights",
]
