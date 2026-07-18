"""Search open deals by deal, company, or primary-contact identity.

This tool provides fast deal search functionality for agents. It returns compact
deal rows with identity, value, stage, pipeline, and company context.

USE CASE:
When a user mentions a deal by name (e.g., "Tell me about the Acme deal"),
use this tool first with only the identifying text (e.g., "Acme") to find the
deal_id, then use get_deal_details for full info.

RETURNS:
- deal_id: UUID of the deal
- name: Deal name
- amount / predicted_amount / currency: Deal value context
- stage_id / stage_name: Current stage context
- pipeline_id / pipeline_name: Current pipeline context
- company_id: UUID of the associated company/account
- company_name: Associated company name

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. It does NOT accept organisation_id as a parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.deals.read_service import DealReadService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def search_deals(
    *,
    context: AgentContext,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search open deals by identifying text.

    Returns compact data for fast lookup and disambiguation.
    Use this to find deal IDs, then use get_deal_details for full info.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        context: AgentContext containing organisation metadata
        query: Identifying deal, company, or primary-contact text only. Do not
            pass the full user request or instruction words.
        limit: Maximum results; defaults to and is capped at 10.

    Returns:
        List of deal dictionaries with identity, value, stage, pipeline, and company context.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("search_deals: missing organisation_id in context")
        return []

    if not query or not query.strip():
        logger.warning("search_deals: empty query provided")
        return []

    effective_limit = max(1, min(limit, 10))

    async with context.acquire_session() as session:
        try:
            rows = await DealReadService(
                session,
                organisation_id,
            ).search_deals_for_agent(
                q=query,
                limit=effective_limit,
            )
        except Exception as e:
            logger.exception(f"search_deals: database error: {e}")
            return []

        output: list[dict[str, Any]] = []
        for row in rows:
            deal = row["deal"]
            output.append(
                {
                    "deal_id": str(deal.deal_id),
                    "name": deal.name,
                    "amount": float(deal.amount) if deal.amount is not None else None,
                    "predicted_amount": float(deal.predicted_amount)
                    if deal.predicted_amount is not None
                    else None,
                    "currency": deal.currency,
                    "deal_status": (
                        deal.deal_status.value
                        if hasattr(deal.deal_status, "value")
                        else deal.deal_status
                    ),
                    "stage_id": str(deal.stage_id) if deal.stage_id else None,
                    "stage_name": deal.stage_name,
                    "pipeline_id": str(deal.pipeline_id) if deal.pipeline_id else None,
                    "pipeline_name": deal.pipeline_name,
                    "company_id": str(deal.company_id) if deal.company_id else None,
                    "company_name": deal.company_name,
                    "primary_contact_name": deal.primary_contact_name,
                    "primary_contact_email": deal.primary_contact_email,
                    "match": row.get("match"),
                }
            )

    logger.info("search_deals: found %d deals matching '%s'", len(output), query)
    return output


def create_search_deals(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create search_deals tool bound to a context.

    SECURITY: organisation_id comes from context (immutable).
    """

    async def search_deals_tool(
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search open deals by identifying text.

        Use this tool first when the user mentions a deal by name.
        Returns deal identity, value, stage, pipeline, and company context for matching deals.
        Then use get_deal_details to get full information.

        Args:
            query: Identifying deal, company, or primary-contact text only.
                Extract this from the user's request; do not pass the full request.
            limit: Maximum results; defaults to and is capped at 10.

        Example: User says "Tell me about the Acme deal"
        → Call search_deals(query="Acme") to find the deal_id
        → Call get_deal_details(deal_id=...) for full info
        """
        logger.info("[TOOL CALLED] search_deals - query='%s', limit=%d", query, limit)

        if not context.organisation_id:
            logger.warning("search_deals unavailable: missing organisation_id in context")
            return []

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SearchDeals",
            tool_func=search_deals,
            parameters={
                "context": context,
                "query": query,
                "limit": limit,
            },
            description=f"Searching deals: {query}",
            fallback_value=[],
        )

        logger.info("[TOOL RESULT] search_deals returned %d deals", len(result) if result else 0)
        return list(result) if result else []

    return search_deals_tool


__all__ = [
    "search_deals",
    "create_search_deals",
]
