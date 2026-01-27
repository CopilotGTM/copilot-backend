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

import httpx

from src.agents.tools.base import execute_with_capabilities
from src.infra.config.configs import linkedin_enrichment_config

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# API Configuration
CLADO_BASE_URL = linkedin_enrichment_config.CLADO_BASE_URL
API_TIMEOUT_SECONDS = 30


def _get_api_key() -> str | None:
    """Get the Clado API key from config."""
    return linkedin_enrichment_config.LINKEDIN_CLADO_API_KEY


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


async def _search_linkedin(
    api_key: str,
    name: str,
    company: str | None = None,
) -> dict[str, Any] | None:
    """Search for a person on LinkedIn."""
    query_parts = [name]
    if company:
        query_parts.append(f"at {company}")

    query = " ".join(query_parts)

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{CLADO_BASE_URL}/api/search",
                params={"query": query, "limit": 3},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            # Handle nested profile structure
            if "profile" in data:
                profile = data.get("profile", {})
                return {
                    "linkedin_url": profile.get("linkedin_url"),
                    "name": profile.get("name"),
                    "headline": profile.get("headline"),
                    "location": profile.get("location"),
                }

            # Handle results array
            results = data.get("results", [])
            if results:
                first = results[0]
                profile = first.get("profile", first)
                return {
                    "linkedin_url": profile.get("linkedin_url"),
                    "name": profile.get("name"),
                    "headline": profile.get("headline"),
                    "location": profile.get("location"),
                }

            return None

    except Exception as e:
        logger.warning("LinkedIn search failed: %s", e)
        return None


async def _get_linkedin_profile(
    api_key: str,
    linkedin_url: str,
) -> dict[str, Any] | None:
    """Get full LinkedIn profile data."""
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{CLADO_BASE_URL}/api/enrich/linkedin",
                params={"linkedin_url": linkedin_url},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            # Normalize nested profile structure
            profile = data.get("profile", {})
            experience = data.get("experience", [])
            education = data.get("education", [])

            if profile:
                return {
                    "name": profile.get("name"),
                    "headline": profile.get("headline"),
                    "location": profile.get("location"),
                    "description": profile.get("description"),
                    "linkedin_url": profile.get("linkedin_url"),
                    "skills": profile.get("skills", []),
                    "connections_count": profile.get("connections_count"),
                    "is_decision_maker": profile.get("is_decision_maker", False),
                    "experience": _format_experience(experience),
                    "education": _format_education(education),
                }

            return data

    except Exception as e:
        logger.warning("LinkedIn profile fetch failed: %s", e)
        return None


def _format_experience(experience: list) -> list[dict[str, Any]]:
    """Format experience data for readability."""
    if not experience or not isinstance(experience, list):
        return []

    formatted = []
    for exp in experience[:5]:  # Limit to 5 most recent
        if isinstance(exp, dict):
            formatted.append(
                {
                    "title": exp.get("title") or exp.get("position"),
                    "company": exp.get("company") or exp.get("company_name") or exp.get("org"),
                    "duration": exp.get("duration") or exp.get("date_range"),
                    "location": exp.get("location"),
                }
            )
    return formatted


def _format_education(education: list) -> list[dict[str, Any]]:
    """Format education data for readability."""
    if not education or not isinstance(education, list):
        return []

    formatted = []
    for edu in education[:3]:  # Limit to 3
        if isinstance(edu, dict):
            formatted.append(
                {
                    "school": edu.get("school") or edu.get("institution"),
                    "degree": edu.get("degree") or edu.get("degree_name"),
                    "field": edu.get("field_of_study") or edu.get("field"),
                    "years": edu.get("date_range") or edu.get("years"),
                }
            )
    return formatted


async def research_linkedin_profile(
    query: str,
    company: str | None = None,
) -> dict[str, Any]:
    """Research a LinkedIn profile by name or URL.

    Args:
        query: Either a LinkedIn URL or a person's name to search for
        company: Optional company name to narrow search (only used if query is a name)

    Returns:
        Dictionary with profile research results
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("research_linkedin_profile: API key not configured")
        return {"success": False, "error": "LinkedIn research not configured"}

    linkedin_url: str | None = None
    search_result: dict[str, Any] | None = None

    # Check if query is a LinkedIn URL
    if _is_linkedin_url(query):
        linkedin_url = _normalize_linkedin_url(query)
        if not linkedin_url:
            return {"success": False, "error": "Invalid LinkedIn URL format"}
    else:
        # Search by name
        search_result = await _search_linkedin(api_key, query, company)
        if not search_result:
            return {
                "success": False,
                "error": f"Could not find LinkedIn profile for '{query}'",
                "suggestion": "Try providing the LinkedIn URL directly or a more specific name",
            }
        linkedin_url = search_result.get("linkedin_url")

    if not linkedin_url:
        return {
            "success": False,
            "error": "Could not determine LinkedIn URL",
            "search_result": search_result,
        }

    # Get full profile
    profile = await _get_linkedin_profile(api_key, linkedin_url)
    if not profile:
        # Return search result if profile fetch fails
        if search_result:
            return {
                "success": True,
                "partial": True,
                "linkedin_url": linkedin_url,
                "profile": search_result,
                "note": "Full profile fetch failed, showing search results only",
            }
        return {"success": False, "error": "Failed to retrieve profile data"}

    return {
        "success": True,
        "linkedin_url": linkedin_url,
        "profile": profile,
    }


def create_research_linkedin_profile(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that creates the LinkedIn research tool.

    This tool is for general research purposes and does NOT:
    - Require organisation context
    - Update any database records

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
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
            - profile: Profile information including:
                - name: Full name
                - headline: Professional headline/title
                - location: Geographic location
                - description: About/summary section
                - skills: List of skills
                - experience: Work history (up to 5 recent positions)
                - education: Education history
                - is_decision_maker: Whether they appear to be a decision maker
            - error: Error message if failed

        Examples:
            # Research by LinkedIn URL
            research_linkedin_profile("https://linkedin.com/in/john-smith")

            # Research by name
            research_linkedin_profile("John Smith")

            # Research by name with company hint
            research_linkedin_profile("John Smith", company="Google")
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
            description=f"Researching LinkedIn profile: {query}",
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

        return dict(result) if result else {}

    return research_linkedin_profile_tool


__all__ = [
    "research_linkedin_profile",
    "create_research_linkedin_profile",
]
