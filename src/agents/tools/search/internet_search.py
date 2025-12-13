"""Internet search tool using DuckDuckGo.

Provides web search capability without requiring API keys.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from duckduckgo_search import DDGS

from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def internet_search(
    query: str,
    *,
    max_results: int = 5,
    region: str | None = "us-en",
    timeout: int = 10,
) -> list[dict[str, str]]:
    """Perform a web search using DuckDuckGo (free, no API key required).

    Args:
        query: Search query string
        max_results: Maximum number of results to return (default: 5, max: 20)
        region: Region code (default: "us-en" for US English)
        timeout: Request timeout in seconds (default: 10)

    Returns:
        List of dictionaries with keys ``label``, ``url``, and ``snippet``.
        Empty list if query is empty or search fails.
    """
    trimmed_query = (query or "").strip()
    if not trimmed_query:
        logger.debug("InternetSearch skipped: empty query provided")
        return []

    effective_max = max(1, min(max_results, 20))

    try:
        ddgs = DDGS(timeout=timeout)
        results_raw = ddgs.text(trimmed_query, max_results=effective_max, region=region)

        results: list[dict[str, str]] = []
        for item in results_raw or []:
            if not isinstance(item, dict):
                continue

            title = item.get("title")
            url = item.get("href")
            snippet = item.get("body")

            if not url:
                continue

            results.append(
                {
                    "label": title or url,
                    "url": url,
                    "snippet": snippet or "",
                }
            )

        if not results:
            logger.info("InternetSearch returned no results for query: '%s'", trimmed_query)

        return results

    except Exception as exc:  # pragma: no cover - network/library dependency
        logger.warning("InternetSearch failed: %s", exc)
        return []


def create_search_internet(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable:
    """Factory that returns a tool function bound to capabilities.

    Note: Internet search doesn't require AgentContext since it doesn't
    access organisation data - it's a stateless web search.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

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

        result = await execute_with_capabilities(
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


__all__ = ["internet_search", "create_search_internet"]
