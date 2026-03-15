"""Arxiv search tool for technical research.

Provides academic paper search capability without requiring API keys.
Useful for researching technical topics, AI/ML concepts, and academic
papers relevant to enterprise deals with technical components.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import arxiv  # type: ignore[import-untyped]

from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def arxiv_search(
    query: str,
    *,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """Search Arxiv for academic papers on a topic.

    Args:
        query: Search query string (technical topic, research area, etc.)
        max_results: Maximum number of results to return (default: 5, max: 20)

    Returns:
        List of dictionaries with paper information.
        Empty list if query is empty or search fails.
    """
    trimmed_query = (query or "").strip()
    if not trimmed_query:
        logger.debug("ArxivSearch skipped: empty query provided")
        return []

    effective_max = max(1, min(max_results, 20))

    try:
        # Create search client
        client = arxiv.Client()
        search = arxiv.Search(
            query=trimmed_query,
            max_results=effective_max,
            sort_by=arxiv.SortCriterion.Relevance,
        )

        results: list[dict[str, Any]] = []
        for paper in client.results(search):
            # Format authors (limit to first 3)
            authors = [author.name for author in paper.authors[:3]]
            if len(paper.authors) > 3:
                authors.append(f"+ {len(paper.authors) - 3} more")

            # Get categories
            categories = paper.categories[:3] if paper.categories else []

            result: dict[str, Any] = {
                "title": paper.title,
                "url": paper.entry_id,
                "pdf_url": paper.pdf_url,
                "authors": authors,
                "published": paper.published.strftime("%Y-%m-%d") if paper.published else None,
                "categories": categories,
                "summary": (
                    paper.summary[:500] + "..." if len(paper.summary) > 500 else paper.summary
                ),
            }

            # Remove None values
            result = {k: v for k, v in result.items() if v is not None}
            results.append(result)

        if not results:
            logger.info("ArxivSearch returned no results for query: '%s'", trimmed_query)

        return results

    except Exception as exc:
        logger.warning("ArxivSearch failed: %s", exc)
        return []


def create_search_arxiv(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[list[dict[str, Any]]]]:
    """Factory that returns an Arxiv search tool function bound to capabilities.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def search_arxiv(query: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Search Arxiv for academic papers and research on a technical topic.

        Use this tool when you need to research technical topics, find academic
        papers, or understand cutting-edge research in areas like AI, machine
        learning, computer science, or other scientific fields. Helpful when
        prospects are in research-heavy industries or discussing technical
        evaluations.

        Args:
            query: The search query - can be a technical topic, research area,
                   algorithm name, or any academic subject you want to research.
            max_results: Maximum number of papers to return (default: 5, max: 20).

        Returns:
            List of dictionaries containing paper information with keys:
            - title: Paper title
            - url: Link to the Arxiv page
            - pdf_url: Direct link to PDF
            - authors: List of author names
            - published: Publication date
            - categories: Arxiv categories
            - summary: Paper abstract (truncated)
        """
        logger.info(f"[TOOL CALLED] search_arxiv - query='{query}', max_results={max_results}")

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ArxivSearch",
            tool_func=arxiv_search,
            parameters={"query": query, "max_results": max_results},
            description=f"Searching research papers: {query}",
            fallback_value=[],
        )

        logger.info(f"[TOOL RESULT] search_arxiv returned {len(result) if result else 0} papers")
        return list(result) if result else []

    return search_arxiv


__all__ = ["arxiv_search", "create_search_arxiv"]
