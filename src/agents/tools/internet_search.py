from __future__ import annotations

import logging

from duckduckgo_search import DDGS

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
        List of dictionaries with keys ``title``, ``url``, and ``snippet``.
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
