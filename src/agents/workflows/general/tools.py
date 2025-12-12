from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar

from src.agents.common.tool_wrappers import use_tool_with_capabilities
from src.agents.context import AgentContext
from src.agents.tools.search import internal_knowledge_base, internet_search
from src.agents.tools.transparent_tools import AgentCapabilities
from src.infra.utils.logger import get_logger

logger = get_logger()

# Context variable to store per-context lock
# This ensures each async context (event loop) gets its own lock
_db_lock_var: ContextVar[asyncio.Lock | None] = ContextVar("general_db_session_lock", default=None)


def _get_db_session_lock():
    """Get or create a database session lock for the current async context."""
    lock = _db_lock_var.get()
    if lock is None:
        lock = asyncio.Lock()
        _db_lock_var.set(lock)
    return lock


def create_search_internal_knowledge(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable:
    """Factory that returns a tool function bound to context and capabilities."""

    async def search_internal_knowledge(query: str, limit: int = 5) -> list[dict[str, str]]:
        """Search internal knowledge base for company documents and information.

        Use this tool to find information from the organization's internal documents,
        knowledge base articles, product documentation, and other internal resources.
        Returns relevant document snippets with citations.

        Args:
            query: The search query to find relevant internal documents.
            limit: Maximum number of results to return (default: 5).

        Returns:
            List of dictionaries containing document information with keys:
            - label: Document name or title
            - url: Internal reference link (kb://org/{org_id}/doc/{doc_id})
            - snippet: Relevant text excerpt from the document
        """
        logger.info(f"[TOOL CALLED] search_internal_knowledge - query='{query}', limit={limit}")

        if not context.session or not context.organisation_id:
            logger.warning("InternalKnowledgeBase unavailable: missing session or organisation_id")
            return []

        # Use lock to prevent concurrent session access
        # Agno may call multiple tools in parallel, but AsyncSession doesn't support that
        async with _get_db_session_lock():
            result = await use_tool_with_capabilities(
                capabilities_getter(),
                tool_name="InternalKnowledgeBase",
                tool_func=internal_knowledge_base,
                parameters={
                    "session": context.session,
                    "organisation_id": context.organisation_id,
                    "query": query,
                    "limit": limit,
                    "user_id": context.user_id,
                },
                description=f"Searching internal documents for: {query}",
                fallback_value=[],
            )
        logger.info(
            f"[TOOL RESULT] search_internal_knowledge returned {len(result) if result else 0} results"
        )
        return list(result) if result else []

    return search_internal_knowledge


def create_search_internet(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable:
    """Factory that returns a tool function bound to capabilities."""

    async def search_internet(query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Search the internet for current information using DuckDuckGo.

        Use this tool when you need up-to-date information from the web,
        news, or publicly available data that may not be in internal documents.

        Args:
            query: The search query to find relevant web results.
            max_results: Maximum number of results to return (default: 5, max: 20).

        Returns:
            List of dictionaries containing web search results with keys:
            - label: Page title
            - url: Full URL to the web page
            - snippet: Text excerpt from the page
        """
        logger.info(f"[TOOL CALLED] search_internet - query='{query}', max_results={max_results}")

        result = await use_tool_with_capabilities(
            capabilities_getter(),
            tool_name="InternetSearch",
            tool_func=internet_search,
            parameters={"query": query, "max_results": max_results},
            description=f"Searching the web for: {query}",
            fallback_value=[],
        )
        logger.info(
            f"[TOOL RESULT] search_internet returned {len(result) if result else 0} results"
        )
        return list(result) if result else []

    return search_internet
