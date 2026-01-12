"""Web crawler tool using Crawl4AI.

Provides web page content extraction without requiring API keys.
Useful for researching company websites, press releases, and news articles.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def crawl_webpage(
    url: str,
    *,
    max_content_length: int = 5000,
) -> dict[str, Any]:
    """Crawl a webpage and extract its content.

    Args:
        url: The URL to crawl (must be a valid HTTP/HTTPS URL)
        max_content_length: Maximum content length to return (default: 5000 chars)

    Returns:
        Dictionary with page content and metadata, or empty dict on failure.
    """
    trimmed_url = (url or "").strip()
    if not trimmed_url:
        logger.debug("CrawlWebpage skipped: empty URL provided")
        return {}

    # Validate URL format
    try:
        parsed = urlparse(trimmed_url)
        if not parsed.scheme:
            trimmed_url = "https://" + trimmed_url
            parsed = urlparse(trimmed_url)
        if parsed.scheme not in ("http", "https"):
            logger.warning("CrawlWebpage: invalid URL scheme: %s", parsed.scheme)
            return {}
        if not parsed.netloc:
            logger.warning("CrawlWebpage: invalid URL - no domain")
            return {}
    except Exception as e:
        logger.warning("CrawlWebpage: invalid URL format: %s", e)
        return {}

    try:
        from crawl4ai import AsyncWebCrawler

        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(url=trimmed_url)

            if not result.success:
                logger.info("CrawlWebpage failed for URL: '%s'", trimmed_url)
                return {}

            # Extract content - convert to string if needed
            raw_content = result.markdown or result.cleaned_html or ""
            content = str(raw_content) if raw_content else ""
            if len(content) > max_content_length:
                content = content[:max_content_length] + "..."

            output: dict[str, Any] = {
                "url": trimmed_url,
                "title": result.metadata.get("title", "") if result.metadata else "",
                "content": content,
            }

            # Add metadata if available
            if result.metadata:
                if result.metadata.get("description"):
                    output["description"] = result.metadata["description"]
                if result.metadata.get("author"):
                    output["author"] = result.metadata["author"]

            # Add links if available (limited to first 10)
            if result.links and isinstance(result.links, dict):
                internal_links = result.links.get("internal", [])[:5]
                external_links = result.links.get("external", [])[:5]
                if internal_links or external_links:
                    output["links"] = {
                        "internal": internal_links,
                        "external": external_links,
                    }

            return output

    except ImportError:
        logger.warning("CrawlWebpage: crawl4ai not installed")
        return {}
    except Exception as exc:
        logger.warning("CrawlWebpage failed for '%s': %s", trimmed_url, exc)
        return {}


def create_crawl_webpage(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that returns a web crawler tool function bound to capabilities.

    Note: Web crawler doesn't require AgentContext since it doesn't
    access organisation data - it fetches external web pages.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def crawl_webpage_tool(url: str) -> dict[str, Any]:
        """Crawl a webpage and extract its content for research.

        Use this tool when you need to read the content of a specific web page -
        such as a company's about page, a press release, a news article, or any
        other publicly accessible web content. This is more thorough than a web
        search when you already know the specific URL you want to analyze.

        Args:
            url: The URL to crawl (e.g., "https://company.com/about",
                 "https://news.site.com/article"). Will add https:// if missing.

        Returns:
            Dictionary containing:
            - url: The crawled URL
            - title: Page title
            - content: Main text content (markdown format, truncated to 5000 chars)
            - description: Meta description if available
            - links: Internal and external links found on the page
        """
        logger.info(f"[TOOL CALLED] crawl_webpage - url='{url}'")

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CrawlWebpage",
            tool_func=crawl_webpage,
            parameters={"url": url},
            description=f"Crawling webpage: {url}",
            fallback_value={},
        )

        logger.info(f"[TOOL RESULT] crawl_webpage returned {len(result) if result else 0} fields")
        return dict(result) if result else {}

    return crawl_webpage_tool


__all__ = ["crawl_webpage", "create_crawl_webpage"]
