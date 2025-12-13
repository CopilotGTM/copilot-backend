"""List deal stakeholders tool for agents.

Tool for listing stakeholders associated with a deal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
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
    deal_id: str,
    *,
    context: AgentContext,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List all stakeholders associated with a deal.

    Returns stakeholder summary information useful for understanding
    the buying committee and key contacts for a deal.

    Args:
        deal_id: The deal UUID as a string
        context: AgentContext containing session and organisation metadata
        limit: Maximum number of stakeholders to return (default: 20)

    Returns:
        List of stakeholder dictionaries with summary information.
    """
    session = context.session
    organisation_id = context.organisation_id

    if not session or not organisation_id:
        logger.warning("list_deal_stakeholders: missing session or organisation_id")
        return []

    # Parse deal_id
    try:
        parsed_deal_id = UUID(deal_id)
    except ValueError:
        logger.warning(f"list_deal_stakeholders: invalid deal_id format: {deal_id}")
        return []

    effective_limit = max(1, min(limit, 50))

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    c = cast(Any, DealStakeholder).__table__.c

    stmt = (
        select(DealStakeholder)
        .where(
            c.organisation_id == organisation_id,
            c.deal_id == parsed_deal_id,
        )
        .order_by(
            c.decision_power.asc(),
            c.engagement_score.desc(),
        )
        .limit(effective_limit)
    )

    try:
        result = await session.execute(stmt)
        stakeholders = result.scalars().all()
    except Exception as e:
        logger.exception(f"list_deal_stakeholders: database error: {e}")
        return []

    # Transform to output format
    output: list[dict[str, Any]] = []
    for s in stakeholders:
        output.append(
            {
                "stakeholder_id": str(s.stakeholder_id),
                "email": s.email,
                "name": s.name,
                "job_title": s.job_title,
                "department": s.department,
                "stance": s.stance.value if isinstance(s.stance, StakeholderStance) else s.stance,
                "decision_power": s.decision_power.value
                if isinstance(s.decision_power, DecisionPower)
                else s.decision_power,
                "temperature": s.temperature.value
                if isinstance(s.temperature, StakeholderTemperature)
                else s.temperature,
                "engagement_score": s.engagement_score,
                "last_contact_at": s.last_contact_at.isoformat() if s.last_contact_at else None,
                "source": s.source.value if isinstance(s.source, StakeholderSource) else s.source,
            }
        )

    logger.info(f"list_deal_stakeholders: found {len(output)} stakeholders for deal {deal_id}")
    return output


def create_list_deal_stakeholders(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_stakeholders tool bound to a context."""

    async def list_deal_stakeholders_tool(
        deal_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List all stakeholders (contacts) associated with a deal.

        Use this tool to understand the buying committee and key people involved
        in a deal. Returns summary information about each stakeholder including
        their name, role, stance, decision power, and engagement level.

        Args:
            deal_id: The deal UUID to get stakeholders for. If not provided,
                     uses the deal from the current context.
            limit: Maximum number of results (default: 20, max: 50)
        """
        logger.info(f"[TOOL CALLED] list_deal_stakeholders - deal_id='{deal_id}', limit={limit}")

        if not context.session or not context.organisation_id:
            logger.warning("list_deal_stakeholders unavailable: missing session or organisation_id")
            return []

        effective_deal_id = deal_id or context.deal_id
        if not effective_deal_id:
            logger.warning("list_deal_stakeholders: no deal_id provided and none in context")
            return []

        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="ListDealStakeholders",
                tool_func=list_deal_stakeholders,
                parameters={
                    "deal_id": effective_deal_id,
                    "context": context,
                    "limit": limit,
                },
                description=f"Listing stakeholders for deal: {effective_deal_id}",
                fallback_value=[],
            )

        logger.info(
            f"[TOOL RESULT] list_deal_stakeholders returned {len(result) if result else 0} stakeholders"
        )
        return list(result) if result else []

    return list_deal_stakeholders_tool
