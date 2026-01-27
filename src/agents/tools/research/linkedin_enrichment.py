"""LinkedIn enrichment tool using Clado API.

Provides LinkedIn profile enrichment for stakeholder research.
Requires LINKEDIN_CLADO_API_KEY environment variable.

API Documentation: https://docs.clado.ai/api-reference/introduction

Credit costs:
- Get LinkedIn Profile: 1 credit
- Scrape LinkedIn Profile: 2 credits
- Get Contact (email): 4 credits (only if found)
- Get Contact (phone): 10 credits (only if found)
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

# API endpoints
CLADO_BASE_URL = linkedin_enrichment_config.CLADO_BASE_URL
ENRICH_PROFILE_ENDPOINT = "/api/enrich/linkedin"
SCRAPE_PROFILE_ENDPOINT = "/api/enrich/scrape"
CONTACT_INFO_ENDPOINT = "/api/enrich/contacts"
CREDITS_ENDPOINT = "/api/credits"

# Timeout for API calls
API_TIMEOUT_SECONDS = 30


def _get_api_key() -> str | None:
    """Get the Clado API key from config."""
    return linkedin_enrichment_config.LINKEDIN_CLADO_API_KEY


def _validate_linkedin_url(url: str) -> str | None:
    """Validate and normalize a LinkedIn URL.

    Args:
        url: The URL to validate

    Returns:
        Normalized LinkedIn URL or None if invalid
    """
    if not url:
        return None

    url = url.strip()

    # Add https if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        # Must be linkedin.com domain
        if "linkedin.com" not in parsed.netloc.lower():
            return None
        # Must have a path (profile ID)
        if not parsed.path or parsed.path == "/":
            return None
        return url
    except Exception:
        return None


async def _make_clado_request(
    endpoint: str,
    params: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    """Make a request to the Clado API.

    Args:
        endpoint: API endpoint path
        params: Query parameters
        api_key: Clado API key

    Returns:
        API response as dictionary

    Raises:
        httpx.HTTPStatusError: On API errors
    """
    url = f"{CLADO_BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


async def get_linkedin_profile(
    linkedin_url: str,
) -> dict[str, Any]:
    """Get LinkedIn profile information using Clado API.

    This retrieves cached/indexed profile data (1 credit).
    For fresh data, use scrape_linkedin_profile instead.

    Args:
        linkedin_url: LinkedIn profile URL (e.g., https://linkedin.com/in/john-smith)

    Returns:
        Dictionary with profile data or error information
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("LinkedIn enrichment: LINKEDIN_CLADO_API_KEY not configured")
        return {"error": "API key not configured", "success": False}

    validated_url = _validate_linkedin_url(linkedin_url)
    if not validated_url:
        logger.warning("LinkedIn enrichment: invalid LinkedIn URL: %s", linkedin_url)
        return {"error": "Invalid LinkedIn URL", "success": False}

    try:
        result = await _make_clado_request(
            ENRICH_PROFILE_ENDPOINT,
            {"linkedin_url": validated_url},
            api_key,
        )

        # Check for API-level errors
        if "error" in result:
            logger.warning("LinkedIn enrichment API error: %s", result.get("error"))
            return {"error": result.get("error"), "success": False}

        return {
            "success": True,
            "profile": result,
            "source": "clado_enrich",
            "credits_used": 1,
        }

    except httpx.HTTPStatusError as e:
        error_msg = f"API error: {e.response.status_code}"
        if e.response.status_code == 401:
            error_msg = "Invalid API key"
        elif e.response.status_code == 402:
            error_msg = "Insufficient credits"
        elif e.response.status_code == 429:
            error_msg = "Rate limit exceeded"
        logger.warning("LinkedIn enrichment HTTP error: %s", error_msg)
        return {"error": error_msg, "success": False}
    except Exception as e:
        logger.exception("LinkedIn enrichment unexpected error: %s", e)
        return {"error": str(e), "success": False}


async def scrape_linkedin_profile(
    linkedin_url: str,
) -> dict[str, Any]:
    """Scrape fresh LinkedIn profile data using Clado API.

    This performs a real-time scrape for the latest data (2 credits).
    Includes experience, education, skills, and recent posts.

    Args:
        linkedin_url: LinkedIn profile URL

    Returns:
        Dictionary with detailed profile data or error information
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("LinkedIn scrape: LINKEDIN_CLADO_API_KEY not configured")
        return {"error": "API key not configured", "success": False}

    validated_url = _validate_linkedin_url(linkedin_url)
    if not validated_url:
        logger.warning("LinkedIn scrape: invalid LinkedIn URL: %s", linkedin_url)
        return {"error": "Invalid LinkedIn URL", "success": False}

    try:
        result = await _make_clado_request(
            SCRAPE_PROFILE_ENDPOINT,
            {"linkedin_url": validated_url},
            api_key,
        )

        if "error" in result:
            logger.warning("LinkedIn scrape API error: %s", result.get("error"))
            return {"error": result.get("error"), "success": False}

        return {
            "success": True,
            "profile": result,
            "source": "clado_scrape",
            "credits_used": 2,
        }

    except httpx.HTTPStatusError as e:
        error_msg = f"API error: {e.response.status_code}"
        if e.response.status_code == 401:
            error_msg = "Invalid API key"
        elif e.response.status_code == 402:
            error_msg = "Insufficient credits"
        elif e.response.status_code == 429:
            error_msg = "Rate limit exceeded"
        logger.warning("LinkedIn scrape HTTP error: %s", error_msg)
        return {"error": error_msg, "success": False}
    except Exception as e:
        logger.exception("LinkedIn scrape unexpected error: %s", e)
        return {"error": str(e), "success": False}


async def get_linkedin_contact_info(
    linkedin_url: str,
    *,
    get_email: bool = True,
    get_phone: bool = False,
) -> dict[str, Any]:
    """Get contact information for a LinkedIn profile.

    Args:
        linkedin_url: LinkedIn profile URL
        get_email: Whether to retrieve email (4 credits if found)
        get_phone: Whether to retrieve phone (10 credits if found)

    Returns:
        Dictionary with contact information or error
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("LinkedIn contact: LINKEDIN_CLADO_API_KEY not configured")
        return {"error": "API key not configured", "success": False}

    validated_url = _validate_linkedin_url(linkedin_url)
    if not validated_url:
        logger.warning("LinkedIn contact: invalid LinkedIn URL: %s", linkedin_url)
        return {"error": "Invalid LinkedIn URL", "success": False}

    try:
        params: dict[str, Any] = {"linkedin_url": validated_url}
        if get_email:
            params["email_enrichment"] = "true"
        if get_phone:
            params["phone_enrichment"] = "true"

        result = await _make_clado_request(
            CONTACT_INFO_ENDPOINT,
            params,
            api_key,
        )

        if "error" in result:
            logger.warning("LinkedIn contact API error: %s", result.get("error"))
            return {"error": result.get("error"), "success": False}

        # Calculate credits used based on what was found
        credits_used = 0
        if get_email and result.get("emails"):
            credits_used += 4
        if get_phone and result.get("phones"):
            credits_used += 10

        return {
            "success": True,
            "contacts": result,
            "source": "clado_contacts",
            "credits_used": credits_used,
        }

    except httpx.HTTPStatusError as e:
        error_msg = f"API error: {e.response.status_code}"
        if e.response.status_code == 401:
            error_msg = "Invalid API key"
        elif e.response.status_code == 402:
            error_msg = "Insufficient credits"
        elif e.response.status_code == 429:
            error_msg = "Rate limit exceeded"
        logger.warning("LinkedIn contact HTTP error: %s", error_msg)
        return {"error": error_msg, "success": False}
    except Exception as e:
        logger.exception("LinkedIn contact unexpected error: %s", e)
        return {"error": str(e), "success": False}


async def enrich_linkedin_profile(
    linkedin_url: str,
    *,
    include_contact: bool = False,
    fresh_scrape: bool = False,
) -> dict[str, Any]:
    """Comprehensive LinkedIn profile enrichment.

    This is the main entry point that combines profile data and optionally
    contact information.

    Args:
        linkedin_url: LinkedIn profile URL (e.g., https://linkedin.com/in/john-smith)
        include_contact: Whether to also retrieve email (adds 4 credits if found)
        fresh_scrape: Use real-time scrape (2 credits) vs cached data (1 credit)

    Returns:
        Dictionary with:
        - success: bool
        - profile: Profile data (name, headline, title, experience, education, etc.)
        - contacts: Contact info if requested
        - credits_used: Total credits consumed
        - error: Error message if failed
    """
    # Get profile data
    if fresh_scrape:
        profile_result = await scrape_linkedin_profile(linkedin_url)
    else:
        profile_result = await get_linkedin_profile(linkedin_url)

    if not profile_result.get("success"):
        return profile_result

    total_credits = profile_result.get("credits_used", 0)
    output: dict[str, Any] = {
        "success": True,
        "profile": profile_result.get("profile", {}),
        "source": profile_result.get("source"),
    }

    # Optionally get contact info
    if include_contact:
        contact_result = await get_linkedin_contact_info(linkedin_url, get_email=True)
        if contact_result.get("success"):
            output["contacts"] = contact_result.get("contacts", {})
            total_credits += contact_result.get("credits_used", 0)

    output["credits_used"] = total_credits
    return output


def create_enrich_linkedin_profile(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that returns a LinkedIn enrichment tool function.

    Note: This tool doesn't require AgentContext since it calls
    an external API and doesn't access organisation data directly.

    Args:
        capabilities_getter: Callable that returns current AgentCapabilities

    Returns:
        Async function suitable for use as an agent tool
    """

    async def enrich_linkedin_profile_tool(
        linkedin_url: str,
        include_contact: bool = False,
        fresh_scrape: bool = False,
    ) -> dict[str, Any]:
        """Enrich a LinkedIn profile with detailed professional information.

        Use this tool to get detailed information about a person from their
        LinkedIn profile, including their current role, work history, education,
        and skills. Useful for stakeholder research and understanding contacts.

        Args:
            linkedin_url: LinkedIn profile URL (e.g., "https://linkedin.com/in/john-smith"
                         or just "linkedin.com/in/john-smith")
            include_contact: Set to True to also retrieve email address (costs extra credits).
                           Default: False
            fresh_scrape: Set to True for real-time data (2 credits) vs cached (1 credit).
                         Use fresh_scrape when you need the most up-to-date information.
                         Default: False

        Returns:
            Dictionary containing:
            - success: Whether the enrichment succeeded
            - profile: Profile data including:
                - name, headline, title, location
                - experience: List of work history
                - education: List of education history
                - skills: List of skills
                - summary: Profile summary/about section
            - contacts: Email/phone if include_contact=True
            - credits_used: API credits consumed
            - error: Error message if failed

        Example:
            # Basic profile lookup (1 credit)
            result = enrich_linkedin_profile("https://linkedin.com/in/john-smith")

            # With contact info (1 + 4 credits if email found)
            result = enrich_linkedin_profile(
                "https://linkedin.com/in/john-smith",
                include_contact=True
            )

            # Fresh scrape with all data (2 + 4 credits)
            result = enrich_linkedin_profile(
                "https://linkedin.com/in/john-smith",
                include_contact=True,
                fresh_scrape=True
            )
        """
        logger.info(
            "[TOOL CALLED] enrich_linkedin_profile - url='%s', contact=%s, fresh=%s",
            linkedin_url,
            include_contact,
            fresh_scrape,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="EnrichLinkedInProfile",
            tool_func=enrich_linkedin_profile,
            parameters={
                "linkedin_url": linkedin_url,
                "include_contact": include_contact,
                "fresh_scrape": fresh_scrape,
            },
            description=f"Enriching LinkedIn profile: {linkedin_url}",
            fallback_value={"error": "Tool execution failed", "success": False},
        )

        if result.get("success"):
            profile = result.get("profile", {})
            logger.info(
                "[TOOL RESULT] enrich_linkedin_profile success: name=%s, credits=%s",
                profile.get("name", "unknown"),
                result.get("credits_used", 0),
            )
        else:
            logger.info(
                "[TOOL RESULT] enrich_linkedin_profile failed: %s",
                result.get("error", "unknown error"),
            )

        return dict(result) if result else {}

    return enrich_linkedin_profile_tool


__all__ = [
    "enrich_linkedin_profile",
    "get_linkedin_profile",
    "scrape_linkedin_profile",
    "get_linkedin_contact_info",
    "create_enrich_linkedin_profile",
]
