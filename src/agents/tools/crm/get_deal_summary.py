"""Get Deal Summary Tool - Retrieve AI-generated deal summary.

This tool fetches the AI-generated deal summary for a specific deal.
The summary is stored in CRMDeal.deal_summary_data.

USE CASE:
When a user asks for an overview of a deal, its status, key stakeholders,
recent activity, or overall health assessment.

RETURNS:
- deal_id: The deal UUID
- name: Deal name
- company_name: Associated company
- deal_summary: The AI-generated summary data containing:
  - executive_summary: Brief overview of the deal
  - deal_health: Overall health assessment
  - key_stakeholders: Important people involved
  - recent_activity: Latest activities and communications
  - risks_and_blockers: Identified risks
  - next_steps: Recommended actions
  - And more fields depending on the summary schema

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. organisation_id comes from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_deal_summary(
    deal_id: str,
    *,
    context: AgentContext,
) -> dict[str, Any] | None:
    """Get the AI-generated summary for a deal.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        deal_id: The deal UUID
        context: AgentContext containing organisation metadata

    Returns:
        Dictionary with deal info and summary data, or None if not found.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_deal_summary: missing organisation_id in context")
        return None

    try:
        parsed_deal_id = UUID(deal_id)
    except ValueError:
        logger.warning("get_deal_summary: invalid deal_id format: %s", deal_id)
        return None

    c = cast(Any, CRMDeal).__table__.c

    stmt = select(CRMDeal).where(
        c.organisation_id == organisation_id,
        c.deal_id == parsed_deal_id,
        c.is_deleted.is_(False),
    )

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            deal = result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_deal_summary: database error: {e}")
            return None

        if not deal:
            logger.info("get_deal_summary: deal not found: %s", deal_id)
            return None

        # Build response
        output: dict[str, Any] = {
            "deal_id": str(deal.deal_id),
            "name": deal.name,
            "company_name": deal.company_name,
            "amount": float(deal.amount) if deal.amount else None,
            "currency": deal.currency,
            "deal_status": deal.deal_status,
            "stage_name": deal.stage_name,
        }

        if deal.deal_summary_data:
            output["status"] = "COMPLETED"
            output["deal_summary"] = deal.deal_summary_data
        else:
            output["status"] = "NOT_GENERATED"
            output["deal_summary"] = None
            output["message"] = "Deal summary has not been generated yet"

    logger.info("get_deal_summary: found deal %s (summary: %s)", deal_id, output["status"])
    return output


def create_get_deal_summary(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_deal_summary tool bound to a context.

    SECURITY: organisation_id comes from context (immutable).
    """

    async def get_deal_summary_tool(
        deal_id: str,
    ) -> dict[str, Any] | None:
        """Get the AI-generated summary for a deal.

        Use this tool to get an overview of a deal including health assessment,
        key stakeholders, recent activity, and recommended next steps.

        Args:
            deal_id: The deal UUID (from search_deals or list_deals)

        Returns deal info plus summary containing:
        - executive_summary: Brief deal overview
        - deal_health: Overall health assessment
        - key_stakeholders: Important people involved
        - recent_activity: Latest activities
        - risks_and_blockers: Identified risks
        - next_steps: Recommended actions
        """
        logger.info("[TOOL CALLED] get_deal_summary - deal_id='%s'", deal_id)

        if not context.organisation_id:
            logger.warning("get_deal_summary unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetDealSummary",
            tool_func=get_deal_summary,
            parameters={
                "deal_id": deal_id,
                "context": context,
            },
            description=f"Getting deal summary for: {deal_id}",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] get_deal_summary - status: %s",
                result.get("status", "unknown"),
            )
        else:
            logger.info("[TOOL RESULT] get_deal_summary returned None")

        return result

    return get_deal_summary_tool


__all__ = [
    "get_deal_summary",
    "create_get_deal_summary",
]
