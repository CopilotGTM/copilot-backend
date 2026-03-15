"""Stakeholder enrichment tool using Apollo.io API.

Enriches stakeholder records with LinkedIn profile data including:
- Full name, job title, department
- LinkedIn URL (stored in external_links)
- Work experience

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

from sqlalchemy import select, update

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.research.apollo_client import apollo_enrich_person
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

ENRICHMENT_COOLDOWN_DAYS = 30


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
    credits_used: int,
    raw_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build enrichment metadata for stakeholder_data."""
    metadata: dict[str, Any] = {
        "enriched_at": datetime.now(UTC).isoformat(),
        "enrichment_source": "apollo",
        "credits_used": credits_used,
    }
    if linkedin_url:
        metadata["linkedin_url"] = linkedin_url
    if raw_profile:
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
        "Engineering": [
            "engineer",
            "developer",
            "architect",
            "cto",
            "technical",
            "devops",
            "sre",
        ],
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


def _extract_domain_hint(email: str | None) -> str | None:
    """Extract company domain from email, skipping personal providers."""
    if not email or "@" not in email:
        return None
    domain = email.split("@")[1].lower()
    personal = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}
    if domain in personal:
        return None
    return domain


async def enrich_stakeholder(
    *,
    context: AgentContext,
    stakeholder_id: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company_name: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Enrich a stakeholder with LinkedIn profile data via Apollo API."""
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("enrich_stakeholder: missing organisation_id")
        return {"success": False, "error": "Missing organisation_id"}

    if not stakeholder_id and not email:
        return {"success": False, "error": "Must provide stakeholder_id or email"}

    c = cast(Any, DealStakeholder).__table__.c

    # Phase 1: Read stakeholder, extract local vars, release session
    try:
        async with context.acquire_session() as session:
            if stakeholder_id:
                stmt = select(DealStakeholder).where(
                    c.organisation_id == organisation_id,
                    c.stakeholder_id == UUID(stakeholder_id),
                )
            else:
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

            # Extract all needed data into local vars before releasing session
            s_id = stakeholder.stakeholder_id
            s_name = stakeholder.name
            s_job_title = stakeholder.job_title
            s_department = stakeholder.department
            s_email = stakeholder.email
            s_external_links = stakeholder.external_links or []
            s_stakeholder_data = stakeholder.stakeholder_data or {}
            search_email = email or s_email
            search_name = name or s_name
            search_company = company_name
            linkedin_url = _extract_linkedin_url(stakeholder)
            domain_hint = _extract_domain_hint(search_email)
        # Session released here before the external API call

    except Exception as e:
        logger.exception("enrich_stakeholder read phase failed: %s", e)
        return {"success": False, "error": str(e)}

    # Phase 2: Call Apollo API (no DB session held)
    apollo_result = await apollo_enrich_person(
        name=search_name,
        email=search_email,
        organization_name=search_company,
        domain=domain_hint,
        linkedin_url=linkedin_url,
    )

    credits_used = 1
    profile_data = apollo_result if apollo_result.get("success") else None

    if profile_data:
        linkedin_url = profile_data.get("linkedin_url") or linkedin_url

    # Prepare updates
    updates: dict[str, Any] = {}

    if profile_data:
        profile_name = profile_data.get("name")
        if profile_name and (not s_name or len(profile_name) > len(s_name)):
            updates["name"] = profile_name

        profile_title = profile_data.get("title") or profile_data.get("headline")
        if profile_title:
            updates["job_title"] = profile_title

        if not s_department:
            department = _infer_department(profile_title)
            if department:
                updates["department"] = department

    if linkedin_url:
        has_linkedin = any(
            isinstance(link, dict) and link.get("platform", "").lower() == "linkedin"
            for link in s_external_links
        )
        if not has_linkedin:
            new_link = {
                "platform": "linkedin",
                "url": linkedin_url,
                "label": "LinkedIn",
                "added_at": datetime.now(UTC).isoformat(),
            }
            updates["external_links"] = s_external_links + [new_link]

    s_stakeholder_data["enrichment"] = _build_enrichment_metadata(
        linkedin_url=linkedin_url,
        credits_used=credits_used,
        raw_profile=profile_data,
    )
    updates["stakeholder_data"] = s_stakeholder_data
    updates["updated_at"] = datetime.now(UTC)

    # Phase 3: Write updates back (fresh session)
    try:
        async with context.acquire_session() as session:
            if updates:
                update_stmt = (
                    update(DealStakeholder)
                    .where(
                        c.organisation_id == organisation_id,
                        c.stakeholder_id == s_id,
                    )
                    .values(**updates)
                )
                await session.execute(update_stmt)
                await session.commit()
    except Exception as e:
        logger.exception("enrich_stakeholder write phase failed: %s", e)
        return {"success": False, "error": str(e)}

    logger.info(
        "enrich_stakeholder: enriched stakeholder %s, credits_used=%d",
        s_id,
        credits_used,
    )

    return {
        "success": True,
        "stakeholder_id": str(s_id),
        "already_enriched": False,
        "data": {
            "name": updates.get("name", s_name),
            "job_title": updates.get("job_title", s_job_title),
            "department": updates.get("department", s_department),
            "linkedin_url": linkedin_url,
        },
        "metadata": {
            "enriched_at": datetime.now(UTC).isoformat(),
            "enrichment_source": "apollo",
            "credits_used": credits_used,
            "profile_found": profile_data is not None,
        },
    }


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
        data (job title, department), and update the stakeholder record.

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
            - data: Enriched profile data (name, job_title, department, linkedin_url)
            - metadata: Enrichment details (source, credits used, timestamp)
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
            description="Enriching stakeholder profile",
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
