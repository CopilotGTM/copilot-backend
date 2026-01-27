"""Stakeholder enrichment tool using Clado API.

Enriches stakeholder records with LinkedIn profile data including:
- Full name, job title, department
- LinkedIn URL (stored in external_links)
- Skills/keywords
- Work experience and education

Tracks enrichment status in stakeholder_data to avoid re-processing.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Optional - if provided, scopes to deal stakeholders
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx
from sqlalchemy import select, update

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.infra.config.configs import linkedin_enrichment_config

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# API Configuration
CLADO_BASE_URL = linkedin_enrichment_config.CLADO_BASE_URL
API_TIMEOUT_SECONDS = 30

# Enrichment settings
ENRICHMENT_COOLDOWN_DAYS = 30  # Don't re-enrich if enriched within this period


def _get_api_key() -> str | None:
    """Get the Clado API key from config."""
    return linkedin_enrichment_config.LINKEDIN_CLADO_API_KEY


async def _clado_search_person(
    api_key: str,
    *,
    email: str | None = None,
    name: str | None = None,
    company: str | None = None,
) -> dict[str, Any] | None:
    """Search for a person on LinkedIn using Clado API.

    Args:
        api_key: Clado API key
        email: Email address to search
        name: Person's name
        company: Company name

    Returns:
        Search result with LinkedIn URL or None if not found.
        Returns normalized structure with 'linkedin_url' at top level.
    """
    # Build search query
    query_parts = []
    if name:
        query_parts.append(name)
    if company:
        query_parts.append(f"at {company}")
    # Extract domain for company hint if no company provided
    if email and not company and "@" in email:
        domain = email.split("@")[1]
        if not domain.endswith((".gmail.com", ".yahoo.com", ".hotmail.com", ".outlook.com")):
            query_parts.append(f"at {domain.split('.')[0]}")

    if not query_parts:
        return None

    query = " ".join(query_parts)

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{CLADO_BASE_URL}/api/search",
                params={"query": query, "limit": 5},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            # Handle response format - Clado returns data nested under 'profile'
            # or as a list of 'results'
            results = data.get("results", [])

            # If no results array, check if it's a single profile response
            if not results and "profile" in data:
                profile = data.get("profile", {})
                return {
                    "linkedin_url": profile.get("linkedin_url"),
                    "name": profile.get("name"),
                    "title": profile.get("headline"),
                    "location": profile.get("location"),
                    "raw_profile": profile,
                }

            if not results:
                return None

            # If we have email, try to match
            if email:
                email_lower = email.lower()
                for res in results:
                    # Check both top-level and nested profile structure
                    result_emails = res.get("emails", [])
                    profile = res.get("profile", {})
                    if not result_emails and profile:
                        result_emails = profile.get("emails", [])
                    if any(e.lower() == email_lower for e in result_emails):
                        return _normalize_search_result(res)

            # Return first result if no email match
            if results:
                return _normalize_search_result(results[0])

            return None

    except Exception as e:
        logger.warning("Clado search failed: %s", e)
        return None


def _normalize_search_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize Clado search result to consistent format.

    Clado API may return data at top level or nested under 'profile'.
    This normalizes to a consistent structure with linkedin_url at top level.
    """
    # Check if data is nested under 'profile'
    profile = result.get("profile", {})
    if profile:
        return {
            "linkedin_url": profile.get("linkedin_url"),
            "name": profile.get("name"),
            "title": profile.get("headline"),
            "location": profile.get("location"),
            "skills": profile.get("skills", []),
            "raw_profile": profile,
        }
    # Data is at top level
    return {
        "linkedin_url": result.get("linkedin_url") or result.get("linkedin_profile_url"),
        "name": result.get("name"),
        "title": result.get("title") or result.get("headline"),
        "location": result.get("location"),
        "skills": result.get("skills", []),
        "raw_profile": result,
    }


async def _clado_get_profile(
    api_key: str,
    linkedin_url: str,
) -> dict[str, Any] | None:
    """Get LinkedIn profile data using Clado API.

    Args:
        api_key: Clado API key
        linkedin_url: LinkedIn profile URL

    Returns:
        Normalized profile data or None if failed.
        Returns structure with name, title, skills etc. at top level.
    """
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{CLADO_BASE_URL}/api/enrich/linkedin",
                params={"linkedin_url": linkedin_url},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()

            # Normalize response - Clado nests data under 'profile'
            profile = result.get("profile", {})
            experience = result.get("experience", [])

            if profile:
                return {
                    "name": profile.get("name"),
                    "title": profile.get("headline"),
                    "headline": profile.get("headline"),
                    "location": profile.get("location"),
                    "description": profile.get("description"),
                    "linkedin_url": profile.get("linkedin_url"),
                    "skills": profile.get("skills", []),
                    "experience": experience,
                    "education": result.get("education", []),
                    "is_decision_maker": profile.get("is_decision_maker", False),
                    "raw_profile": profile,
                }

            # Fallback if not nested
            return result

    except Exception as e:
        logger.warning("Clado profile fetch failed for %s: %s", linkedin_url, e)
        return None


async def _clado_get_contact_by_email(
    api_key: str,
    email: str,
) -> dict[str, Any] | None:
    """Get contact info and LinkedIn URL by email using Clado API.

    Args:
        api_key: Clado API key
        email: Email address

    Returns:
        Contact data including LinkedIn URL or None
    """
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{CLADO_BASE_URL}/api/enrich/contacts",
                params={"email": email},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result

    except Exception as e:
        logger.warning("Clado contact lookup failed for %s: %s", email, e)
        return None


def _extract_linkedin_url(stakeholder: DealStakeholder) -> str | None:
    """Extract LinkedIn URL from stakeholder's external_links."""
    if not stakeholder.external_links:
        return None

    for link in stakeholder.external_links:
        if isinstance(link, dict) and link.get("platform", "").lower() == "linkedin":
            return link.get("url")

    return None


def _is_recently_enriched(stakeholder: DealStakeholder) -> bool:
    """Check if stakeholder was enriched recently."""
    if not stakeholder.stakeholder_data:
        return False

    enrichment = stakeholder.stakeholder_data.get("enrichment", {})
    enriched_at_str = enrichment.get("enriched_at")

    if not enriched_at_str:
        return False

    try:
        enriched_at = datetime.fromisoformat(enriched_at_str.replace("Z", "+00:00"))
        cooldown = timedelta(days=ENRICHMENT_COOLDOWN_DAYS)
        return datetime.now(UTC) - enriched_at < cooldown
    except Exception:
        return False


def _build_enrichment_metadata(
    linkedin_url: str | None,
    source: str,
    credits_used: int,
    raw_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build enrichment metadata for stakeholder_data."""
    metadata: dict[str, Any] = {
        "enriched_at": datetime.now(UTC).isoformat(),
        "enrichment_source": source,
        "credits_used": credits_used,
    }
    if linkedin_url:
        metadata["linkedin_url"] = linkedin_url
    if raw_profile:
        # Store subset of raw profile for reference
        metadata["profile_snapshot"] = {
            "name": raw_profile.get("name"),
            "headline": raw_profile.get("headline"),
            "title": raw_profile.get("title"),
            "location": raw_profile.get("location"),
        }
    return metadata


def _infer_department(job_title: str | None) -> str | None:
    """Infer department from job title."""
    if not job_title:
        return None

    title_lower = job_title.lower()

    department_keywords = {
        "Engineering": ["engineer", "developer", "architect", "cto", "technical", "devops", "sre"],
        "Sales": ["sales", "account executive", "ae", "sdr", "bdr", "revenue"],
        "Marketing": ["marketing", "growth", "brand", "cmo", "demand gen"],
        "Product": ["product", "pm", "product manager", "product owner"],
        "Finance": ["finance", "cfo", "accounting", "controller", "treasurer"],
        "Legal": ["legal", "counsel", "attorney", "compliance", "general counsel"],
        "HR": ["hr", "human resources", "people", "talent", "recruiting"],
        "Operations": ["operations", "coo", "ops", "supply chain"],
        "IT": ["it ", "information technology", "infrastructure", "cio"],
        "Security": ["security", "ciso", "infosec", "cyber"],
        "Customer Success": ["customer success", "cs ", "csm", "support"],
        "Executive": ["ceo", "founder", "president", "chief executive"],
    }

    for department, keywords in department_keywords.items():
        if any(kw in title_lower for kw in keywords):
            return department

    return None


async def enrich_stakeholder(
    *,
    context: AgentContext,
    stakeholder_id: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company_name: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Enrich a stakeholder with LinkedIn profile data.

    Looks up the stakeholder, finds their LinkedIn profile via Clado API,
    and updates the stakeholder record with enriched data.

    Args:
        context: AgentContext with organisation_id
        stakeholder_id: UUID of stakeholder to enrich (preferred)
        email: Email to find stakeholder (if stakeholder_id not provided)
        name: Name hint for LinkedIn search
        company_name: Company hint for LinkedIn search
        force_refresh: Re-enrich even if recently enriched

    Returns:
        Dictionary with enrichment results
    """
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("enrich_stakeholder: missing organisation_id")
        return {"success": False, "error": "Missing organisation_id"}

    api_key = _get_api_key()
    if not api_key:
        logger.warning("enrich_stakeholder: LINKEDIN_CLADO_API_KEY not configured")
        return {"success": False, "error": "LinkedIn API not configured"}

    # Need either stakeholder_id or email
    if not stakeholder_id and not email:
        return {"success": False, "error": "Must provide stakeholder_id or email"}

    c = cast(Any, DealStakeholder).__table__.c

    async with context.acquire_session() as session:
        try:
            # Find stakeholder
            if stakeholder_id:
                stmt = select(DealStakeholder).where(
                    c.organisation_id == organisation_id,
                    c.stakeholder_id == UUID(stakeholder_id),
                )
            else:
                # email is guaranteed non-None here due to earlier check
                assert email is not None
                stmt = select(DealStakeholder).where(
                    c.organisation_id == organisation_id,
                    c.email == email.lower().strip(),
                )
                if deal_id:
                    stmt = stmt.where(c.deal_id == deal_id)

            result = await session.execute(stmt)
            stakeholder = result.scalar_one_or_none()

            if not stakeholder:
                return {
                    "success": False,
                    "error": "Stakeholder not found",
                    "stakeholder_id": stakeholder_id,
                    "email": email,
                }

            # Check if recently enriched
            if not force_refresh and _is_recently_enriched(stakeholder):
                logger.info(
                    "enrich_stakeholder: skipping recently enriched stakeholder %s",
                    stakeholder.stakeholder_id,
                )
                existing_enrichment = (stakeholder.stakeholder_data or {}).get("enrichment", {})
                return {
                    "success": True,
                    "stakeholder_id": str(stakeholder.stakeholder_id),
                    "already_enriched": True,
                    "enriched_at": existing_enrichment.get("enriched_at"),
                    "data": {
                        "name": stakeholder.name,
                        "job_title": stakeholder.job_title,
                        "department": stakeholder.department,
                        "linkedin_url": _extract_linkedin_url(stakeholder),
                    },
                }

            # Get search parameters from stakeholder
            search_email = email or stakeholder.email
            search_name = name or stakeholder.name
            search_company = company_name

            credits_used = 0
            linkedin_url = _extract_linkedin_url(stakeholder)
            profile_data: dict[str, Any] | None = None

            # Step 1: Find LinkedIn URL if not already known
            if not linkedin_url:
                # NOTE: Contact lookup commented out - requires additional Clado credits (402)
                # To re-enable, uncomment the following block:
                # ----
                # # Try contact lookup by email first (most accurate)
                # contact_result = await _clado_get_contact_by_email(api_key, search_email)
                # if contact_result:
                #     credits_used += 4  # Contact lookup cost
                #     # Extract LinkedIn URL from social links
                #     social_links = contact_result.get("socialLinks", [])
                #     for link in social_links:
                #         if "linkedin.com" in link.get("url", ""):
                #             linkedin_url = link.get("url")
                #             break
                # ----

                # Search by name + company
                search_result = await _clado_search_person(
                    api_key,
                    email=search_email,
                    name=search_name,
                    company=search_company,
                )
                if search_result:
                    credits_used += 1  # Search cost per result
                    linkedin_url = search_result.get("linkedin_url") or search_result.get(
                        "linkedin_profile_url"
                    )
                    logger.info(
                        "enrich_stakeholder: found LinkedIn URL via search: %s",
                        linkedin_url,
                    )

            # Step 2: Get profile data if we have LinkedIn URL
            if linkedin_url:
                profile_data = await _clado_get_profile(api_key, linkedin_url)
                if profile_data:
                    credits_used += 1  # Profile lookup cost

            # Step 3: Prepare updates
            updates: dict[str, Any] = {}

            if profile_data:
                # Update name if we got a better one
                profile_name = profile_data.get("name")
                if profile_name and (
                    not stakeholder.name or len(profile_name) > len(stakeholder.name)
                ):
                    updates["name"] = profile_name

                # Update job title
                profile_title = profile_data.get("title") or profile_data.get("headline")
                if profile_title:
                    updates["job_title"] = profile_title

                # Infer department
                if not stakeholder.department:
                    department = _infer_department(profile_title)
                    if department:
                        updates["department"] = department

                # Update keywords from skills
                skills = profile_data.get("skills", [])
                if skills and isinstance(skills, list):
                    updates["keywords"] = skills[:20]  # Limit to 20 skills

            # Update external_links with LinkedIn URL
            if linkedin_url:
                existing_links = stakeholder.external_links or []
                # Check if LinkedIn already exists
                has_linkedin = any(
                    isinstance(link, dict) and link.get("platform", "").lower() == "linkedin"
                    for link in existing_links
                )
                if not has_linkedin:
                    new_link = {
                        "platform": "linkedin",
                        "url": linkedin_url,
                        "label": "LinkedIn",
                        "added_at": datetime.now(UTC).isoformat(),
                    }
                    updates["external_links"] = existing_links + [new_link]

            # Update stakeholder_data with enrichment metadata
            existing_data = stakeholder.stakeholder_data or {}
            existing_data["enrichment"] = _build_enrichment_metadata(
                linkedin_url=linkedin_url,
                source="clado",
                credits_used=credits_used,
                raw_profile=profile_data,
            )
            updates["stakeholder_data"] = existing_data
            updates["updated_at"] = datetime.now(UTC)

            # Step 4: Apply updates
            if updates:
                update_stmt = (
                    update(DealStakeholder)
                    .where(c.stakeholder_id == stakeholder.stakeholder_id)
                    .values(**updates)
                )
                await session.execute(update_stmt)
                await session.commit()

            logger.info(
                "enrich_stakeholder: enriched stakeholder %s, credits_used=%d",
                stakeholder.stakeholder_id,
                credits_used,
            )

            return {
                "success": True,
                "stakeholder_id": str(stakeholder.stakeholder_id),
                "already_enriched": False,
                "data": {
                    "name": updates.get("name", stakeholder.name),
                    "job_title": updates.get("job_title", stakeholder.job_title),
                    "department": updates.get("department", stakeholder.department),
                    "linkedin_url": linkedin_url,
                    "keywords": updates.get("keywords", stakeholder.keywords),
                },
                "metadata": {
                    "enriched_at": datetime.now(UTC).isoformat(),
                    "enrichment_source": "clado",
                    "credits_used": credits_used,
                    "profile_found": profile_data is not None,
                },
            }

        except Exception as e:
            logger.exception("enrich_stakeholder failed: %s", e)
            await session.rollback()
            return {"success": False, "error": str(e)}


def create_enrich_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the enrich_stakeholder tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Optional scope to deal stakeholders
    """

    async def enrich_stakeholder_tool(
        stakeholder_id: str | None = None,
        email: str | None = None,
        name: str | None = None,
        company_name: str | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Enrich a stakeholder with LinkedIn profile data.

        Use this tool to get detailed professional information about a stakeholder
        from LinkedIn. The tool will find their LinkedIn profile, extract relevant
        data (job title, department, skills), and update the stakeholder record.

        The tool tracks enrichment status - if a stakeholder was enriched recently
        (within 30 days), it returns cached data unless force_refresh=True.

        Args:
            stakeholder_id: UUID of the stakeholder to enrich (preferred method)
            email: Email address to find the stakeholder (if stakeholder_id not provided)
            name: Full name to help find LinkedIn profile (optional, improves accuracy)
            company_name: Company name to help find LinkedIn profile (optional)
            force_refresh: Set to True to re-enrich even if recently enriched

        Returns:
            Dictionary containing:
            - success: Whether enrichment succeeded
            - stakeholder_id: UUID of the stakeholder
            - already_enriched: True if returning cached data
            - data: Enriched profile data
                - name: Full name
                - job_title: Current job title
                - department: Inferred department
                - linkedin_url: LinkedIn profile URL
                - keywords: Skills/expertise
            - metadata: Enrichment details (source, credits used, timestamp)

        Example:
            # Enrich by stakeholder ID (preferred)
            result = enrich_stakeholder(stakeholder_id="uuid-here")

            # Enrich by email with hints
            result = enrich_stakeholder(
                email="john@acme.com",
                name="John Smith",
                company_name="Acme Corp"
            )

            # Force re-enrichment
            result = enrich_stakeholder(stakeholder_id="uuid", force_refresh=True)
        """
        logger.info(
            "[TOOL CALLED] enrich_stakeholder - stakeholder_id=%s, email=%s",
            stakeholder_id,
            email,
        )

        if not context.organisation_id:
            logger.warning("enrich_stakeholder unavailable: missing organisation_id")
            return {"success": False, "error": "Missing organisation context"}

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="EnrichStakeholder",
            tool_func=enrich_stakeholder,
            parameters={
                "context": context,
                "stakeholder_id": stakeholder_id,
                "email": email,
                "name": name,
                "company_name": company_name,
                "force_refresh": force_refresh,
            },
            description=f"Enriching stakeholder: {stakeholder_id or email}",
            fallback_value={"success": False, "error": "Tool execution failed"},
        )

        if result.get("success"):
            logger.info(
                "[TOOL RESULT] enrich_stakeholder success: stakeholder=%s, credits=%s",
                result.get("stakeholder_id"),
                result.get("metadata", {}).get("credits_used", 0),
            )
        else:
            logger.info(
                "[TOOL RESULT] enrich_stakeholder failed: %s",
                result.get("error", "unknown"),
            )

        return dict(result) if result else {"success": False, "error": "No result"}

    return enrich_stakeholder_tool


__all__ = [
    "enrich_stakeholder",
    "create_enrich_stakeholder",
]
