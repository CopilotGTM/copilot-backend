"""Wikipedia search tool for research.

Provides Wikipedia search capability without requiring API keys.
Useful for researching companies, executives, industries, and technologies.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import wikipedia

from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def wikipedia_search(
    query: str,
    *,
    limit: int = 3,
) -> list[dict[str, str]]:
    """Search Wikipedia for information on a topic.

    Args:
        query: Search query string (company name, person, technology, etc.)
        limit: Maximum number of results to return (default: 3, max: 10)

    Returns:
        List of dictionaries with keys ``label``, ``url``, and ``snippet``.
        Empty list if query is empty or search fails.
    """
    trimmed_query = (query or "").strip()
    if not trimmed_query:
        logger.debug("WikipediaSearch skipped: empty query provided")
        return []

    effective_limit = max(1, min(limit, 10))

    try:
        # Search for matching Wikipedia pages
        search_results = wikipedia.search(trimmed_query, results=effective_limit)

        if not search_results:
            logger.info("WikipediaSearch returned no results for query: '%s'", trimmed_query)
            return []

        results: list[dict[str, str]] = []
        for title in search_results:
            try:
                # Get page summary (first few sentences)
                page = wikipedia.page(title, auto_suggest=False)
                summary = wikipedia.summary(title, sentences=2, auto_suggest=False)

                results.append(
                    {
                        "label": page.title,
                        "url": page.url,
                        "snippet": summary[:500] + "..." if len(summary) > 500 else summary,
                    }
                )
            except wikipedia.exceptions.DisambiguationError as e:
                # Handle disambiguation pages - take first option
                if e.options:
                    try:
                        first_option = e.options[0]
                        page = wikipedia.page(first_option, auto_suggest=False)
                        summary = wikipedia.summary(first_option, sentences=2, auto_suggest=False)
                        results.append(
                            {
                                "label": page.title,
                                "url": page.url,
                                "snippet": summary[:500] + "..." if len(summary) > 500 else summary,
                            }
                        )
                    except Exception as inner_e:
                        logger.debug("WikipediaSearch disambiguation failed: %s", inner_e)
                        continue
            except wikipedia.exceptions.PageError:
                # Page doesn't exist, skip
                continue
            except Exception as e:
                logger.debug("WikipediaSearch failed for '%s': %s", title, e)
                continue

        return results

    except Exception as exc:
        logger.warning("WikipediaSearch failed: %s", exc)
        return []


def create_search_wikipedia(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[list[dict[str, str]]]]:
    """Factory that returns a Wikipedia search tool function bound to capabilities.

    Note: Wikipedia search doesn't require AgentContext since it doesn't
    access organisation data - it's a stateless web lookup.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def search_wikipedia(query: str, limit: int = 3) -> list[dict[str, str]]:
        """Search Wikipedia for background information on a topic.

        Use this tool when you need general knowledge or background information
        about companies, people, industries, technologies, or concepts. Great for
        researching prospect companies, understanding industry terminology, or
        learning about key executives before meetings.

        Args:
            query: The search query - can be a company name, person, technology,
                   industry term, or any topic you want to research.
            limit: Maximum number of results to return (default: 3, max: 10).

        Returns:
            List of dictionaries containing Wikipedia article info with keys:
            - label: Article title
            - url: Full URL to the Wikipedia page
            - snippet: Summary excerpt from the article
        """
        logger.info(f"[TOOL CALLED] search_wikipedia - query='{query}', limit={limit}")

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="WikipediaSearch",
            tool_func=wikipedia_search,
            parameters={"query": query, "limit": limit},
            description=f"Searching Wikipedia: {query}",
            fallback_value=[],
        )

        logger.info(
            f"[TOOL RESULT] search_wikipedia returned {len(result) if result else 0} results"
        )
        return list(result) if result else []

    return search_wikipedia


__all__ = ["wikipedia_search", "create_search_wikipedia"]
