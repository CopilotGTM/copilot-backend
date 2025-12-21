"""Get existing risks and blockers tool for agents.

Tool for retrieving existing risks and blockers for a deal,
used by the extraction agent for deduplication.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_existing_risks_blockers(
    deal_id: str,
    *,
    context: AgentContext,
    include_dismissed: bool = False,
) -> list[dict[str, Any]]:
    """Get all existing risks and blockers for a deal.

    Used by the extraction agent to check for duplicates before
    creating new AI-suggested items.

    Args:
        deal_id: The deal UUID as a string
        context: AgentContext containing session and organisation metadata
        include_dismissed: Whether to include dismissed/AI_dismissed items

    Returns:
        List of risk/blocker dictionaries with relevant fields for matching.
    """
    session = context.session
    organisation_id = context.organisation_id

    if not session or not organisation_id:
        logger.warning("get_existing_risks_blockers: missing session or organisation_id")
        return []

    # Parse deal_id
    try:
        parsed_deal_id = UUID(deal_id)
    except ValueError:
        logger.warning(f"get_existing_risks_blockers: invalid deal_id format: {deal_id}")
        return []

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    c = cast(Any, DealRiskBlocker).__table__.c

    stmt = select(DealRiskBlocker).where(
        c.organisation_id == organisation_id,
        c.deal_id == parsed_deal_id,
    )

    # Optionally exclude dismissed items
    if not include_dismissed:
        stmt = stmt.where(
            c.status.not_in(
                [
                    RiskBlockerStatus.DISMISSED.value,
                    RiskBlockerStatus.AI_DISMISSED.value,
                ]
            )
        )

    stmt = stmt.order_by(c.created_at.desc())

    try:
        result = await session.execute(stmt)
        items = result.scalars().all()
    except Exception as e:
        logger.exception(f"get_existing_risks_blockers: database error: {e}")
        return []

    # Transform to output format optimized for deduplication
    output: list[dict[str, Any]] = []
    for item in items:
        output.append(
            {
                "risk_blocker_id": str(item.risk_blocker_id),
                "item_type": item.item_type.value
                if isinstance(item.item_type, RiskBlockerType)
                else item.item_type,
                "category": item.category.value
                if isinstance(item.category, RiskBlockerCategory)
                else item.category,
                "severity": item.severity.value
                if isinstance(item.severity, RiskBlockerSeverity)
                else item.severity,
                "status": item.status.value
                if isinstance(item.status, RiskBlockerStatus)
                else item.status,
                "title": item.title,
                "description": item.description,
                "quote": item.quote,
                "source_type": item.source_type.value
                if isinstance(item.source_type, RiskBlockerSource)
                else item.source_type,
                "source_record_id": item.source_record_id,
                "is_ai_generated": item.is_ai_generated,
                "ai_reasoning": item.ai_reasoning,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
        )

    logger.info(f"get_existing_risks_blockers: found {len(output)} items for deal {deal_id}")
    return output


def create_get_existing_risks_blockers(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_existing_risks_blockers tool bound to a context."""

    async def get_existing_risks_blockers_tool(
        deal_id: str | None = None,
        include_dismissed: bool = False,
    ) -> list[dict[str, Any]]:
        """Get all existing risks and blockers for a deal.

        Use this tool to check what risks and blockers already exist for a deal
        before suggesting new ones. This helps avoid creating duplicates.

        Each item includes:
        - item_type: RISK or BLOCKER
        - title: Short description of the issue
        - description: Detailed description
        - quote: Supporting quote from source content
        - status: Current status (OPEN, IN_PROGRESS, RESOLVED, etc.)
        - category: Category of the issue
        - severity: CRITICAL, HIGH, MEDIUM, or LOW
        - source_type: Where it was identified (MEETING, EMAIL, SLACK, etc.)

        Args:
            deal_id: The deal UUID. If not provided, uses deal from context.
            include_dismissed: Include dismissed items (default: false)
        """
        logger.info(
            f"[TOOL CALLED] get_existing_risks_blockers - deal_id='{deal_id}', "
            f"include_dismissed={include_dismissed}"
        )

        if not context.session or not context.organisation_id:
            logger.warning(
                "get_existing_risks_blockers unavailable: missing session or organisation_id"
            )
            return []

        effective_deal_id = deal_id or context.deal_id
        if not effective_deal_id:
            logger.warning("get_existing_risks_blockers: no deal_id provided and none in context")
            return []

        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="GetExistingRisksBlockers",
                tool_func=get_existing_risks_blockers,
                parameters={
                    "deal_id": effective_deal_id,
                    "context": context,
                    "include_dismissed": include_dismissed,
                },
                description=f"Getting existing risks/blockers for deal: {effective_deal_id}",
                fallback_value=[],
            )

        logger.info(
            f"[TOOL RESULT] get_existing_risks_blockers returned {len(result) if result else 0} items"
        )
        return list(result) if result else []

    return get_existing_risks_blockers_tool
