"""List deal stakeholders tool for agents.

Tool for listing stakeholders associated with a deal.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Always from context — prevents cross-org data access

deal_id can be passed as an optional parameter. If not provided, falls back
to context.deal_id. This allows the tool to be used both in deal-scoped
views and in cross-deal queries (e.g. from search_deals results).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.insights.participant_stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_stakeholders(
    *,
    context: AgentContext,
    deal_id: str | None = None,
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
    # SECURITY: organisation_id always from context (immutable)
    organisation_id = context.organisation_id
    # deal_id: parameter overrides context
    actual_deal_id = UUID(deal_id) if deal_id else context.deal_id

    if not organisation_id:
        logger.warning("list_deal_stakeholders: missing organisation_id in context")
        return []

    if not actual_deal_id:
        logger.warning("list_deal_stakeholders: no deal_id provided and none in context")
        return []

    effective_limit = max(1, min(limit, 50))

    c = cast(Any, CRMDealParticipant).__table__.c

    # SECURITY: Both organisation_id and deal_id from context (immutable)
    stmt = (
        select(CRMDealParticipant)
        .where(
            c.organisation_id == organisation_id,
            c.deal_id == actual_deal_id,
            c.participant_type == CRMParticipantType.CONTACT.value,
            c.is_deleted.is_(False),
        )
        .order_by(
            c.decision_power.asc(),
            c.related_name.asc().nullslast(),
        )
        .limit(effective_limit)
    )

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            participants = result.scalars().all()
        except Exception as e:
            logger.exception(f"list_deal_stakeholders: database error: {e}")
            return []

        # Transform to output format (while session is active)
        output: list[dict[str, Any]] = []
        for participant in participants:
            payload = participant_to_stakeholder_detail_dict(participant)
            output.append(
                {
                    "participant_id": str(payload["participant_id"]),
                    "email": payload["email"],
                    "name": payload["name"],
                    "job_title": payload["job_title"],
                    "phone_number": payload["phone_number"],
                    "department": payload["department"],
                    "stance": payload["stance"],
                    "decision_power": payload["decision_power"],
                    "temperature": payload["temperature"],
                    "last_contact_at": payload["last_contact_at"].isoformat()
                    if payload["last_contact_at"]
                    else None,
                    "source": payload["source"],
                }
            )
    # Session released

    logger.info(
        "list_deal_stakeholders: found %d stakeholders for deal %s", len(output), actual_deal_id
    )
    return output


def create_list_deal_stakeholders(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_stakeholders tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Always from context — prevents cross-org data access

    deal_id can be passed as an optional parameter. If not provided, falls back
    to context.deal_id.
    """

    async def list_deal_stakeholders_tool(
        deal_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List all stakeholders (contacts) associated with a deal.

        Use this tool to understand the buying committee and key people involved
        in the deal. Returns summary information about each stakeholder including
        their name, role, stance, decision power, and engagement level.

        Args:
            deal_id: Deal UUID to query. If not provided, uses the current deal
                context. Pass this when you got a deal_id from search_deals but
                are not in a deal view.
            limit: Maximum number of results (default: 20, max: 50)

        Note: organisation_id is automatically set from context and cannot be
        overridden.
        """
        logger.info("[TOOL CALLED] list_deal_stakeholders - limit=%d", limit)

        if not context.organisation_id:
            logger.warning("list_deal_stakeholders unavailable: missing organisation_id in context")
            return []

        effective_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
        if not effective_deal_id:
            logger.warning("list_deal_stakeholders unavailable: no deal_id provided or in context")
            return []

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealStakeholders",
            tool_func=list_deal_stakeholders,
            parameters={
                "context": context,
                "deal_id": effective_deal_id,
                "limit": limit,
            },
            description="Looking up stakeholders",
            fallback_value=[],
        )

        logger.info(
            "[TOOL RESULT] list_deal_stakeholders returned %d stakeholders",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return list_deal_stakeholders_tool
