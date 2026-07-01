"""Read-only stakeholder research tool via Apollo API.

Looks up a stakeholder in the DB for context (email, name, existing links),
calls Apollo for enrichment data, and returns the result WITHOUT writing
anything to the database. All persistence is handled by the service layer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.enrichment.apollo import apollo_enrich_person

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# These helpers are duplicated from enrich_stakeholder.py to avoid a circular
# import: enrich_stakeholder imports from this package's apollo_client, and this
# package's __init__.py imports research_stakeholder, so we cannot import from
# enrich_stakeholder at module load time. The functions are small and pure.

_PERSONAL_EMAIL_DOMAINS = frozenset({"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"})


def _extract_linkedin_url(links: list[dict[str, Any]] | None) -> str | None:
    """Extract LinkedIn URL from an external links list."""
    if not links:
        return None
    for link in links:
        if isinstance(link, dict) and link.get("platform", "").lower() == "linkedin":
            return link.get("url")
    return None


def _extract_domain_hint(email: str | None) -> str | None:
    """Extract a company domain from an email, skipping personal providers."""
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].lower()
    return None if domain in _PERSONAL_EMAIL_DOMAINS else domain


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized or None


def _contact_external_links(contact: CRMContact | None) -> list[dict[str, Any]] | None:
    if not contact or not isinstance(contact.properties, dict):
        return None
    links = contact.properties.get("external_links")
    return links if isinstance(links, list) else None


async def research_stakeholder(
    *,
    context: AgentContext,
    stakeholder_id: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company_name: str | None = None,
) -> dict[str, Any]:
    """Look up a stakeholder via Apollo and return enrichment data (read-only).

    Reads the participant-backed stakeholder record for lookup context, calls Apollo,
    and returns the profile data. Does NOT write to the database.

    Args:
        context: AgentContext with organisation_id for DB read access.
        stakeholder_id: UUID of the stakeholder (preferred identifier).
        email: Email address (used if no stakeholder_id).
        name: Full name (optional, improves Apollo match accuracy).
        company_name: Company name (optional, improves Apollo match accuracy).

    Returns:
        Dictionary with profile data or error information.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        return {"success": False, "error": "Missing organisation_id"}

    if not stakeholder_id and not email:
        return {"success": False, "error": "Must provide stakeholder_id or email"}

    c = cast(Any, CRMDealParticipant).__table__.c

    # Phase 1: Read stakeholder for lookup context, then release session
    try:
        async with context.acquire_session() as session:
            if stakeholder_id:
                stmt = select(CRMDealParticipant).where(
                    c.organisation_id == organisation_id,
                    c.participant_id == UUID(stakeholder_id),
                    c.participant_type == CRMParticipantType.CONTACT.value,
                    c.is_deleted.is_(False),
                )
            else:
                assert email is not None
                stmt = select(CRMDealParticipant).where(
                    c.organisation_id == organisation_id,
                    c.participant_type == CRMParticipantType.CONTACT.value,
                    c.is_deleted.is_(False),
                    func.lower(func.trim(c.related_email)) == email.lower().strip(),
                )
                if context.deal_id:
                    stmt = stmt.where(c.deal_id == context.deal_id)

            result = await session.execute(stmt)
            participant = result.scalar_one_or_none()

            if not participant:
                return {
                    "success": False,
                    "error": "Stakeholder not found",
                    "participant_id": stakeholder_id,
                    "email": email,
                }

            contact = None
            if participant.related_local_id:
                contact = await session.get(CRMContact, participant.related_local_id)
                if not contact or contact.organisation_id != organisation_id or contact.is_deleted:
                    contact = None

            # Extract lookup context before releasing session
            s_id = participant.participant_id
            s_name = (contact.full_name if contact else None) or participant.related_name
            s_email = _normalize_email(
                (contact.email if contact else None) or participant.related_email
            )
            links = _contact_external_links(contact) or getattr(
                participant,
                "external_links",
                [],
            )
            existing_linkedin = (
                (getattr(contact, "linkedin_url", None) if contact else None)
                or getattr(participant, "linkedin_url", None)
                or _extract_linkedin_url(links)
            )
            domain_hint = _extract_domain_hint(s_email or email)

    except Exception as e:
        logger.exception("research_stakeholder read phase failed: %s", e)
        return {"success": False, "error": str(e)}

    # Phase 2: Call Apollo API (no DB session held)
    search_email = email or s_email
    search_name = name or s_name

    apollo_result = await apollo_enrich_person(
        name=search_name,
        email=search_email,
        organization_name=company_name,
        domain=domain_hint,
        linkedin_url=existing_linkedin,
    )

    profile_data = apollo_result if apollo_result.get("success") else None
    found_linkedin = (
        profile_data.get("linkedin_url") if profile_data else None
    ) or existing_linkedin

    # Phase 3: Return full Apollo data — NO DB writes
    if not profile_data:
        return {
            "success": False,
            "participant_id": str(s_id),
            "error": "No matching person found in Apollo",
        }

    # Pass through the full Apollo profile so the LLM has all available data
    # to populate structured fields and synthesize the markdown profile
    return {
        "success": True,
        "participant_id": str(s_id),
        "data": {
            "apollo_id": profile_data.get("apollo_id"),
            "name": profile_data.get("name"),
            "first_name": profile_data.get("first_name"),
            "last_name": profile_data.get("last_name"),
            "title": profile_data.get("title"),
            "headline": profile_data.get("headline"),
            "linkedin_url": found_linkedin,
            "photo_url": profile_data.get("photo_url"),
            "location": profile_data.get("location"),
            "organization_name": profile_data.get("organization_name"),
            "organization_domain": profile_data.get("organization_domain"),
            "employment_history": profile_data.get("employment_history", []),
        },
    }


def create_research_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that creates the research_stakeholder tool bound to an AgentContext."""

    async def research_stakeholder_tool(
        stakeholder_id: str | None = None,
        email: str | None = None,
        name: str | None = None,
        company_name: str | None = None,
    ) -> dict[str, Any]:
        """Research a stakeholder's professional profile via Apollo (read-only).

        Use this tool to look up professional information about a stakeholder.
        It fetches data from Apollo including name, job title, LinkedIn URL,
        location, and employment history. This tool does NOT update the database.

        Args:
            stakeholder_id: UUID of the stakeholder to research (preferred).
            email: Email address to find the stakeholder (if no stakeholder_id).
            name: Full name to improve search accuracy (optional).
            company_name: Company name to improve search accuracy (optional).

        Returns:
            Dictionary containing:
            - success: Whether the lookup found a match
            - stakeholder_id: UUID of the stakeholder
            - already_enriched: True if returning cached data from a recent lookup
            - data: Profile data (name, job_title, headline, location,
              linkedin_url, organization_name, employment_history)
            - error: Error message if lookup failed
        """
        logger.info(
            "[TOOL CALLED] research_stakeholder - stakeholder_id=%s, email=%s",
            stakeholder_id,
            email,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ResearchStakeholder",
            tool_func=research_stakeholder,
            parameters={
                "context": context,
                "stakeholder_id": stakeholder_id,
                "email": email,
                "name": name,
                "company_name": company_name,
            },
            description="Researching stakeholder profile via Apollo",
            fallback_value={"success": False, "error": "Tool execution failed"},
        )

        if result.get("success"):
            logger.info(
                "[TOOL RESULT] research_stakeholder success: participant=%s",
                result.get("participant_id"),
            )
        else:
            logger.info(
                "[TOOL RESULT] research_stakeholder failed: %s",
                result.get("error", "unknown"),
            )

        return dict(result) if result else {"success": False, "error": "No result"}

    return research_stakeholder_tool


__all__ = [
    "research_stakeholder",
    "create_research_stakeholder",
]
