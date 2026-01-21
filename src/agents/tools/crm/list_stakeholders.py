"""List deal stakeholders tool for agents.

Tool for listing stakeholders associated with a deal.

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
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_stakeholders(
    *,
    context: AgentContext,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List all stakeholders associated with a deal.

    Returns stakeholder summary information useful for understanding
    the buying committee and key contacts for a deal.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    Args:
        context: AgentContext containing organisation and deal metadata
        limit: Maximum number of stakeholders to return (default: 20)

    Returns:
        List of stakeholder dictionaries with summary information.
    """
    # SECURITY: Both organisation_id and deal_id come from context, not parameters
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("list_deal_stakeholders: missing organisation_id in context")
        return []

    if not deal_id:
        logger.warning("list_deal_stakeholders: missing deal_id in context")
        return []

    effective_limit = max(1, min(limit, 50))

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    c = cast(Any, DealStakeholder).__table__.c

    # SECURITY: Both organisation_id and deal_id from context (immutable)
    stmt = (
        select(DealStakeholder)
        .where(
            c.organisation_id == organisation_id,
            c.deal_id == deal_id,
        )
        .order_by(
            c.decision_power.asc(),
            c.engagement_score.desc(),
        )
        .limit(effective_limit)
    )

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            stakeholders = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_deal_stakeholders: database error: {e}")
            return []

        # Transform to output format (while session is active)
        output: list[dict[str, Any]] = []
        for s in stakeholders:
            output.append(
                {
                    "stakeholder_id": str(s.stakeholder_id),
                    "email": s.email,
                    "name": s.name,
                    "job_title": s.job_title,
                    "department": s.department,
                    "stance": s.stance.value
                    if isinstance(s.stance, StakeholderStance)
                    else s.stance,
                    "decision_power": s.decision_power.value
                    if isinstance(s.decision_power, DecisionPower)
                    else s.decision_power,
                    "temperature": s.temperature.value
                    if isinstance(s.temperature, StakeholderTemperature)
                    else s.temperature,
                    "engagement_score": s.engagement_score,
                    "last_contact_at": s.last_contact_at.isoformat() if s.last_contact_at else None,
                    "source": s.source.value
                    if isinstance(s.source, StakeholderSource)
                    else s.source,
                }
            )
    # Session released

    logger.info("list_deal_stakeholders: found %d stakeholders for deal %s", len(output), deal_id)
    return output


def create_list_deal_stakeholders(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_stakeholders tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
    as parameters - both come from context.
    """

    async def list_deal_stakeholders_tool(
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List all stakeholders (contacts) associated with the current deal.

        Use this tool to understand the buying committee and key people involved
        in the deal. Returns summary information about each stakeholder including
        their name, role, stance, decision power, and engagement level.

        Args:
            limit: Maximum number of results (default: 20, max: 50)

        Note: organisation_id and deal_id are automatically set from context
        and cannot be overridden. This tool only queries the current deal.
        """
        logger.info("[TOOL CALLED] list_deal_stakeholders - limit=%d", limit)

        if not context.organisation_id:
            logger.warning("list_deal_stakeholders unavailable: missing organisation_id in context")
            return []

        if not context.deal_id:
            logger.warning("list_deal_stakeholders unavailable: missing deal_id in context")
            return []

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealStakeholders",
            tool_func=list_deal_stakeholders,
            parameters={
                "context": context,
                "limit": limit,
            },
            description=f"Listing stakeholders for deal: {context.deal_id}",
            fallback_value=[],
        )

        logger.info(
            "[TOOL RESULT] list_deal_stakeholders returned %d stakeholders",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return list_deal_stakeholders_tool
