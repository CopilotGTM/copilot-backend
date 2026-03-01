"""LinkedIn enrichment tool using Apollo.io API.

Provides LinkedIn profile enrichment for stakeholder research.
API key is sourced from Azure Key Vault via ApolloConfig.

API Documentation: https://docs.apollo.io/reference/people-enrichment
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.research.apollo_client import apollo_enrich_person

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _validate_linkedin_url(url: str) -> str | None:
    """Validate and normalize a LinkedIn URL."""
    if not url:
        return None

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


async def enrich_linkedin_profile(
    linkedin_url: str,
) -> dict[str, Any]:
    """Enrich a LinkedIn profile via Apollo API.

    Args:
        linkedin_url: LinkedIn profile URL

    Returns:
        Dictionary with profile data or error information
    """
    validated_url = _validate_linkedin_url(linkedin_url)
    if not validated_url:
        logger.warning("LinkedIn enrichment: invalid LinkedIn URL: %s", linkedin_url)
        return {"error": "Invalid LinkedIn URL", "success": False}

    result = await apollo_enrich_person(linkedin_url=validated_url)

    if not result.get("success"):
        return result

    return {
        "success": True,
        "profile": result,
        "source": "apollo",
        "credits_used": 1,
    }


def create_enrich_linkedin_profile(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that returns a LinkedIn enrichment tool function.

    This tool doesn't require AgentContext since it calls
    an external API and doesn't access organisation data directly.
    """

    async def enrich_linkedin_profile_tool(
        linkedin_url: str,
    ) -> dict[str, Any]:
        """Enrich a LinkedIn profile with detailed professional information.

        Use this tool to get detailed information about a person from their
        LinkedIn profile, including their current role and work history.
        Useful for stakeholder research and understanding contacts.

        Args:
            linkedin_url: LinkedIn profile URL (e.g., "https://linkedin.com/in/john-smith"
                         or just "linkedin.com/in/john-smith")

        Returns:
            Dictionary containing:
            - success: Whether the enrichment succeeded
            - profile: Profile data including name, headline, title, location,
              employment_history, linkedin_url, organization_name
            - credits_used: API credits consumed (1 per call)
            - error: Error message if failed
        """
        logger.info(
            "[TOOL CALLED] enrich_linkedin_profile - url='%s'",
            linkedin_url,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="EnrichLinkedInProfile",
            tool_func=enrich_linkedin_profile,
            parameters={"linkedin_url": linkedin_url},
            description=f"Enriching LinkedIn profile: {linkedin_url}",
            fallback_value={"error": "Tool execution failed", "success": False},
        )

        if result.get("success"):
            profile = result.get("profile", {})
            logger.info(
                "[TOOL RESULT] enrich_linkedin_profile success: name=%s",
                profile.get("name", "unknown"),
            )
        else:
            logger.info(
                "[TOOL RESULT] enrich_linkedin_profile failed: %s",
                result.get("error", "unknown error"),
            )

        return dict(result) if result else {"success": False, "error": "No result"}

    return enrich_linkedin_profile_tool


__all__ = [
    "enrich_linkedin_profile",
    "create_enrich_linkedin_profile",
]
