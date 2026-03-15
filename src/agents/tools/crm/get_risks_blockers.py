"""Get existing risks and blockers tool for agents.

Tool for retrieving existing risks and blockers for a deal,
used by the extraction agent for deduplication.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Always from context — prevents cross-org data access
- deal_id: Can be passed as an optional parameter; falls back to context
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
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
    *,
    context: AgentContext,
    deal_id: str | None = None,
    include_dismissed: bool = False,
) -> list[dict[str, Any]]:
    """Get all existing risks and blockers for a deal.

    Used by the extraction agent to check for duplicates before
    creating new AI-suggested items.

    SECURITY BOUNDARIES:
    - organisation_id: Always from context (immutable) — prevents cross-org access
    - deal_id: Optional parameter; falls back to context if not provided

    Args:
        context: AgentContext containing organisation and deal metadata
        deal_id: Optional deal ID override; defaults to context.deal_id
        include_dismissed: Whether to include dismissed/AI_dismissed items

    Returns:
        List of risk/blocker dictionaries with relevant fields for matching.
    """
    # SECURITY: organisation_id always from context (immutable)
    organisation_id = context.organisation_id
    # deal_id: parameter overrides context
    actual_deal_id = UUID(deal_id) if deal_id else context.deal_id

    if not organisation_id:
        logger.warning("get_existing_risks_blockers: missing organisation_id in context")
        return []

    if not actual_deal_id:
        logger.warning("get_existing_risks_blockers: no deal_id provided and none in context")
        return []

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    c = cast(Any, DealRiskBlocker).__table__.c

    # SECURITY: Both organisation_id and deal_id from context (immutable)
    stmt = select(DealRiskBlocker).where(
        c.organisation_id == organisation_id,
        c.deal_id == actual_deal_id,
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

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            items = result.scalars().all()
        except Exception as e:
            logger.exception(f"get_existing_risks_blockers: database error: {e}")
            return []

        # Transform to output format optimized for deduplication (while session is active)
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
    # Session released

    logger.info(
        "get_existing_risks_blockers: found %d items for deal %s", len(output), actual_deal_id
    )
    return output


def create_get_existing_risks_blockers(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_existing_risks_blockers tool bound to a context.

    SECURITY BOUNDARIES:
    - organisation_id: Always from context (immutable) — prevents cross-org access
    - deal_id: Can be passed as an optional parameter; falls back to context
    """

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
            deal_id: Optional deal ID to query. Defaults to the current deal in context.
            include_dismissed: Include dismissed items (default: false)

        Note: organisation_id is always set from context and cannot be overridden.
        """
        logger.info(
            "[TOOL CALLED] get_existing_risks_blockers - include_dismissed=%s",
            include_dismissed,
        )

        if not context.organisation_id:
            logger.warning(
                "get_existing_risks_blockers unavailable: missing organisation_id in context"
            )
            return []

        effective_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
        if not effective_deal_id:
            logger.warning(
                "get_existing_risks_blockers unavailable: no deal_id provided or in context"
            )
            return []

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetExistingRisksBlockers",
            tool_func=get_existing_risks_blockers,
            parameters={
                "context": context,
                "deal_id": effective_deal_id,
                "include_dismissed": include_dismissed,
            },
            description="Reviewing risks and blockers",
            fallback_value=[],
        )

        logger.info(
            "[TOOL RESULT] get_existing_risks_blockers returned %d items",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return get_existing_risks_blockers_tool
