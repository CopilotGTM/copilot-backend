"""LinkedIn profile research tool for general chat.

This tool allows agents to research LinkedIn profiles without updating any database.
It's designed for general research queries like "tell me about this person on LinkedIn".

Unlike enrich_stakeholder, this tool:
- Does NOT require organisation context
- Does NOT update any database records
- Simply returns LinkedIn profile information for research purposes
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.agents.tools.base import execute_with_capabilities
from src.service.enrichment.apollo import apollo_enrich_person

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _is_linkedin_url(text: str) -> bool:
    """Check if the text is a LinkedIn URL."""
    if not text:
        return False
    text = text.strip().lower()
    return "linkedin.com/in/" in text or "linkedin.com/company/" in text


def _normalize_linkedin_url(url: str) -> str | None:
    """Normalize a LinkedIn URL."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        if "linkedin.com" not in parsed.netloc.lower():
            return None
        if not parsed.path or parsed.path == "/":
            return None
        return url
    except Exception:
        return None


async def research_linkedin_profile(
    query: str,
    company: str | None = None,
) -> dict[str, Any]:
    """Research a LinkedIn profile by name or URL via Apollo API.

    Args:
        query: Either a LinkedIn URL or a person's name to search for
        company: Optional company name to narrow search (only used if query is a name)

    Returns:
        Dictionary with profile research results
    """
    # Mode 1: Direct LinkedIn URL
    if _is_linkedin_url(query):
        linkedin_url = _normalize_linkedin_url(query)
        if not linkedin_url:
            return {"success": False, "error": "Invalid LinkedIn URL format"}

        result = await apollo_enrich_person(linkedin_url=linkedin_url)
        if not result.get("success"):
            return result

        return {
            "success": True,
            "linkedin_url": result.get("linkedin_url") or linkedin_url,
            "profile": result,
        }

    # Mode 2: Search by name (+optional company)
    result = await apollo_enrich_person(
        name=query,
        organization_name=company,
    )

    if not result.get("success"):
        return {
            "success": False,
            "error": f"Could not find LinkedIn profile for '{query}'",
            "suggestion": ("Try providing the LinkedIn URL directly or a more specific name"),
        }

    return {
        "success": True,
        "linkedin_url": result.get("linkedin_url"),
        "profile": result,
    }


def create_research_linkedin_profile(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that creates the LinkedIn research tool.

    This tool is for general research purposes and does NOT:
    - Require organisation context
    - Update any database records
    """

    async def research_linkedin_profile_tool(
        query: str,
        company: str | None = None,
    ) -> dict[str, Any]:
        """Research a LinkedIn profile to learn about a person.

        Use this tool when someone asks about a person's professional background,
        or wants to know more about someone on LinkedIn. You can provide either
        a LinkedIn URL or the person's name.

        Args:
            query: Either a LinkedIn profile URL (e.g., "https://linkedin.com/in/john-smith")
                   OR a person's name to search for (e.g., "John Smith")
            company: Optional company name to help find the right person.
                    Only used when query is a name, not a URL.
                    Example: "Acme Corp" or "Google"

        Returns:
            Dictionary containing:
            - success: Whether the research succeeded
            - linkedin_url: The person's LinkedIn profile URL
            - profile: Profile information including name, headline,
              title, location, employment_history, organization_name
            - error: Error message if failed
        """
        logger.info(
            "[TOOL CALLED] research_linkedin_profile - query='%s', company='%s'",
            query,
            company,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ResearchLinkedInProfile",
            tool_func=research_linkedin_profile,
            parameters={"query": query, "company": company},
            description="Researching LinkedIn profile",
            fallback_value={"success": False, "error": "Tool execution failed"},
        )

        if result.get("success"):
            profile = result.get("profile", {})
            logger.info(
                "[TOOL RESULT] research_linkedin_profile success: name=%s",
                profile.get("name", "unknown"),
            )
        else:
            logger.info(
                "[TOOL RESULT] research_linkedin_profile failed: %s",
                result.get("error", "unknown"),
            )

        return dict(result) if result else {"success": False, "error": "No result"}

    return research_linkedin_profile_tool


__all__ = [
    "research_linkedin_profile",
    "create_research_linkedin_profile",
]
