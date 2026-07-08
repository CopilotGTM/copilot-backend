"""Search CRM accounts from chat workflows."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.accounts.read_service import AccountReadService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


async def search_accounts(
    *,
    context: AgentContext,
    query: str | None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search CRM accounts by name or domain within the current organization."""
    organisation_id = context.organisation_id
    cleaned_query = _clean_optional(query)
    if not organisation_id or not cleaned_query:
        return []

    async with context.acquire_session() as org_session:
        service = AccountReadService(org_session, organisation_id)
        try:
            rows = await service.search_accounts_for_agent(
                q=cleaned_query,
                limit=max(1, min(limit, 25)),
            )
        except Exception:
            logger.exception("search_accounts: failed")
            return []

    output: list[dict[str, Any]] = []
    for row in rows:
        company = row["company"]
        output.append(
            {
                "account_id": str(company.company_id),
                "name": company.name,
                "domain": company.domain,
                "website": company.website,
                "industry": company.industry,
                "contact_count": row.get("contact_count", 0),
                "deal_count": row.get("deal_count", 0),
                "match": row.get("match"),
            }
        )
    return output


def create_search_accounts(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the search_accounts tool bound to context."""

    async def search_accounts_tool(
        query: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search CRM accounts by name or domain before creating duplicates."""
        return list(
            await execute_with_capabilities(
                capabilities_getter(),
                tool_name="SearchAccounts",
                tool_func=search_accounts,
                parameters={"context": context, "query": query, "limit": limit},
                description=f"Searching accounts {query or ''}".strip(),
                fallback_value=[],
            )
        )

    return search_accounts_tool


__all__ = ["search_accounts", "create_search_accounts"]
