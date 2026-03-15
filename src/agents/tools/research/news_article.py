"""News article extraction tool using Newspaper4k.

Provides news article parsing and extraction without requiring API keys.
Useful for reading and summarizing news articles about prospects,
competitors, and industry trends.
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


def extract_news_article(
    url: str,
    *,
    max_content_length: int = 5000,
) -> dict[str, Any]:
    """Extract and parse a news article from a URL.

    Args:
        url: The URL of the news article to extract
        max_content_length: Maximum content length to return (default: 5000 chars)

    Returns:
        Dictionary with article content and metadata, or empty dict on failure.
    """
    trimmed_url = (url or "").strip()
    if not trimmed_url:
        logger.debug("ExtractNewsArticle skipped: empty URL provided")
        return {}

    # Validate URL format
    try:
        parsed = urlparse(trimmed_url)
        if not parsed.scheme:
            trimmed_url = "https://" + trimmed_url
            parsed = urlparse(trimmed_url)
        if parsed.scheme not in ("http", "https"):
            logger.warning("ExtractNewsArticle: invalid URL scheme: %s", parsed.scheme)
            return {}
        if not parsed.netloc:
            logger.warning("ExtractNewsArticle: invalid URL - no domain")
            return {}
    except Exception as e:
        logger.warning("ExtractNewsArticle: invalid URL format: %s", e)
        return {}

    try:
        from newspaper import Article

        # Create and download article
        article = Article(trimmed_url)
        article.download()
        article.parse()

        # Try to run NLP for keywords/summary (optional)
        try:
            article.nlp()
            keywords = article.keywords[:10] if article.keywords else []
            summary = article.summary or ""
        except Exception:
            keywords = []
            summary = ""

        # Build result
        content = article.text or ""
        if len(content) > max_content_length:
            content = content[:max_content_length] + "..."

        result: dict[str, Any] = {
            "url": trimmed_url,
            "title": article.title or "",
            "content": content,
            "source": parsed.netloc,
        }

        # Add optional fields if available
        if article.authors:
            result["authors"] = article.authors[:5]  # Limit to 5 authors

        if article.publish_date:
            result["published_date"] = article.publish_date.strftime("%Y-%m-%d")

        if article.top_image:
            result["image_url"] = article.top_image

        if summary:
            result["summary"] = summary[:500] + "..." if len(summary) > 500 else summary

        if keywords:
            result["keywords"] = keywords

        if article.meta_description:
            result["description"] = article.meta_description

        return result

    except ImportError:
        logger.warning("ExtractNewsArticle: newspaper4k not installed")
        return {}
    except Exception as exc:
        logger.warning("ExtractNewsArticle failed for '%s': %s", trimmed_url, exc)
        return {}


def create_extract_news_article(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that returns a news article extraction tool bound to capabilities.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def extract_news_article_tool(url: str) -> dict[str, Any]:
        """Extract and parse a news article from a URL.

        Use this tool when you need to read a news article about a prospect,
        competitor, or industry topic. It extracts the main article content,
        author, publish date, and generates a summary. Better than web crawling
        for news sites as it's optimized for article extraction.

        Args:
            url: The URL of the news article to extract (e.g.,
                 "https://techcrunch.com/article-about-company").

        Returns:
            Dictionary containing:
            - url: The article URL
            - title: Article headline
            - content: Full article text (truncated to 5000 chars)
            - authors: List of article authors
            - published_date: Publication date
            - source: The news source domain
            - summary: AI-generated summary of the article
            - keywords: Extracted keywords from the article
        """
        logger.info(f"[TOOL CALLED] extract_news_article - url='{url}'")

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ExtractNewsArticle",
            tool_func=extract_news_article,
            parameters={"url": url},
            description="Reading news article",
            fallback_value={},
        )

        logger.info(
            f"[TOOL RESULT] extract_news_article returned {len(result) if result else 0} fields"
        )
        return dict(result) if result else {}

    return extract_news_article_tool


__all__ = ["extract_news_article", "create_extract_news_article"]
