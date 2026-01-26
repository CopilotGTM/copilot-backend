"""Tool factories for the General Agent.

This module provides factory functions that create tool functions bound to
the agent's context and capabilities. Tools are created per-agent instance
to ensure proper scoping of organisation_id and other context.
"""

from __future__ import annotations

from collections.abc import Callable

from src.agents.common.tool_wrappers import use_tool_with_capabilities
from src.agents.context import AgentContext
from src.agents.tools.search import internal_knowledge_base, internet_search
from src.agents.tools.transparent_tools import AgentCapabilities
from src.infra.utils.logger import get_logger

logger = get_logger()


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
        logger.info("[TOOL CALLED] search_internal_knowledge - query='%s', limit=%d", query, limit)

        if not context.organisation_id:
            logger.warning("InternalKnowledgeBase unavailable: missing organisation_id")
            return []

        # Use fresh session pattern - internal_knowledge_base will acquire session via context
        result = await use_tool_with_capabilities(
            capabilities_getter(),
            tool_name="InternalKnowledgeBase",
            tool_func=internal_knowledge_base,
            parameters={
                "context": context,  # Pass context instead of session
                "query": query,
                "limit": limit,
            },
            description=f"Searching internal documents for: {query}",
            fallback_value=[],
        )
        logger.info(
            "[TOOL RESULT] search_internal_knowledge returned %d results",
            len(result) if result else 0,
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
        logger.info(
            "[TOOL CALLED] search_internet - query='%s', max_results=%d", query, max_results
        )

        result = await use_tool_with_capabilities(
            capabilities_getter(),
            tool_name="InternetSearch",
            tool_func=internet_search,
            parameters={"query": query, "max_results": max_results},
            description=f"Searching the web for: {query}",
            fallback_value=[],
        )
        logger.info(
            "[TOOL RESULT] search_internet returned %d results",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return search_internet
