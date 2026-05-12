"""Search CRM accounts from chat workflows."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.access.accounts_access_service import AccountsAccessService

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
        service = AccountsAccessService(org_session, organisation_id)
        try:
            rows, _total = await service.list_accounts(
                page=1,
                limit=max(1, min(limit, 25)),
                q=cleaned_query,
            )
        except Exception:
            logger.exception("search_accounts: failed")
            return []

    return [
        {
            "account_id": str(row.get("company_id")),
            "name": row.get("name"),
            "domain": row.get("domain"),
            "website": row.get("website"),
            "industry": row.get("industry"),
            "contact_count": row.get("contact_count", 0),
            "deal_count": row.get("deal_count", 0),
        }
        for row in rows
    ]


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
