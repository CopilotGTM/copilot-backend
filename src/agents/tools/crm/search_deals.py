"""Search Deals Tool - Quick lookup by name or company name.

This tool provides fast deal search functionality for agents. It returns
minimal data (deal_id, name, company_name) optimized for quick lookups.

USE CASE:
When a user mentions a deal by name (e.g., "Tell me about the Acme deal"),
use this tool first to find the deal_id, then use get_deal_details for full info.

RETURNS:
- deal_id: UUID of the deal
- name: Deal name
- company_name: Associated company name

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. It does NOT accept organisation_id as a parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, or_, select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

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
    """Quick search for deals by name or company name.

    Returns minimal data (deal_id, name, company_name) for fast lookup.
    Use this to find deal IDs, then use get_deal_details for full info.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access

    Args:
        context: AgentContext containing organisation metadata
        query: Search term - matches deal name or company name (case-insensitive)
        limit: Maximum results (default: 10, max: 20)

    Returns:
        List of deal dictionaries with deal_id, name, company_name.
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("search_deals: missing organisation_id in context")
        return []

    if not query or not query.strip():
        logger.warning("search_deals: empty query provided")
        return []

    effective_limit = max(1, min(limit, 20))
    search_term = f"%{query.strip()}%"

    c = cast(Any, CRMDeal).__table__.c

    # Search by name or company_name (case-insensitive)
    stmt = (
        select(CRMDeal)
        .where(
            c.organisation_id == organisation_id,
            c.is_deleted.is_(False),
            or_(
                func.lower(c.name).like(func.lower(search_term)),
                func.lower(c.company_name).like(func.lower(search_term)),
            ),
        )
        .order_by(c.source_modified_at.desc().nulls_last())
        .limit(effective_limit)
    )

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            deals = result.scalars().all()
        except Exception as e:
            logger.exception(f"search_deals: database error: {e}")
            return []

        output: list[dict[str, Any]] = []
        for deal in deals:
            output.append(
                {
                    "deal_id": str(deal.deal_id),
                    "name": deal.name,
                    "company_name": deal.company_name,
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
        """Quick search for deals by name or company name.

        Use this tool first when the user mentions a deal by name.
        Returns deal_id, name, and company_name for matching deals.
        Then use get_deal_details to get full information.

        Args:
            query: Search term - matches deal name or company name
            limit: Maximum results (default: 10, max: 20)

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
            description=f"Searching deals for: {query}",
            fallback_value=[],
        )

        logger.info("[TOOL RESULT] search_deals returned %d deals", len(result) if result else 0)
        return list(result) if result else []

    return search_deals_tool


__all__ = [
    "search_deals",
    "create_search_deals",
]
