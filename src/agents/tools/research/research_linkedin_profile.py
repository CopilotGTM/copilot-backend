"""Read-only professional profile research for chat and realtime voice.

The tool reuses tenant-owned CRM enrichment before calling Apollo. Email lookup
requires immutable organisation and user context, and the provider boundary
fails closed unless the address is explicitly known to be external.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from src.agents.tools.base import execute_with_capabilities
from src.api.dependencies.rate_limit import enforce_contact_research_rate_limit
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.research_service import public_profile_enrichment
from src.service.crm.utils.identity import is_valid_email, normalize_email
from src.service.crm.utils.internal_domains import get_internal_domains, is_internal_email
from src.service.enrichment.apollo import apollo_enrich_person

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

MAX_EMAIL_CHARS = 320
MAX_LINKEDIN_URL_CHARS = 500
MAX_COMPANY_CHARS = 200


def _normalize_linkedin_url(value: str) -> str | None:
    """Return a canonical-looking URL only for LinkedIn-owned hostnames."""
    url = value.strip()
    if len(url) > MAX_LINKEDIN_URL_CHARS:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"):
        return None
    if not parsed.path.startswith("/in/") or parsed.path.rstrip("/") == "/in":
        return None
    return url


def _linkedin_url_from_query(query: str | None) -> str | None:
    """Recognize URL-shaped input without treating a plain name as provider identity."""
    if not query:
        return None
    candidate = query.strip()
    if "linkedin.com/" not in candidate.lower():
        return None
    return _normalize_linkedin_url(candidate)


def _sanitized_apollo_profile(result: dict[str, Any]) -> dict[str, Any]:
    """Remove provider evidence fields that are not part of the agent contract."""
    return {
        key: value
        for key, value in result.items()
        if key != "raw" and not key.startswith("_") and key != "success"
    }


async def _find_cached_contact(
    context: AgentContext,
    email: str,
) -> dict[str, Any] | None:
    """Return cached research for an exact tenant-scoped email identity.

    The organisation predicate is the trust boundary; an attendee email by
    itself must never select another tenant's contact.
    """
    contact_c = cast(Any, CRMContact).__table__.c
    stmt = (
        select(CRMContact)
        .where(
            contact_c.organisation_id == context.organisation_id,
            contact_c.is_deleted.is_(False),
            or_(
                func.lower(func.trim(contact_c.email)) == email,
                contact_c.additional_emails.contains([email]),
            ),
        )
        .limit(1)
    )
    async with context.acquire_session() as session:
        result = await session.execute(stmt)
        contact = result.scalars().first()

    if not contact or not (contact.contact_research or contact.profile_enrichment):
        return None

    enriched_at = contact.last_researched_at or contact.profile_enriched_at
    return {
        "success": True,
        "source": "crm_cache",
        "email": email,
        "linkedin_url": contact.linkedin_url,
        "research": contact.contact_research,
        "profile": public_profile_enrichment(contact.profile_enrichment),
        "enriched_at": enriched_at.isoformat() if enriched_at else None,
    }


async def research_linkedin_profile(
    query: str | None = None,
    company: str | None = None,
    *,
    email: str | None = None,
    context: AgentContext | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Research one person by exact attendee email or LinkedIn profile URL.

    Email enrichment is available only to authenticated, tenant-scoped agent
    calls and only for addresses proven external by organisation domains. URL
    lookup remains available for explicit general research and never writes CRM.
    """
    normalized_email = normalize_email(email)
    linkedin_url = _linkedin_url_from_query(query)

    if company is not None and len(company.strip()) > MAX_COMPANY_CHARS:
        return {"success": False, "error": "Company name is too long"}

    if normalized_email:
        if len(normalized_email) > MAX_EMAIL_CHARS or not is_valid_email(normalized_email):
            return {"success": False, "error": "Invalid email address"}
        if not context or not context.organisation_id or not context.user_id:
            return {
                "success": False,
                "error": "Authenticated organisation and user context is required for email research",
            }

        internal_domains = await get_internal_domains(context.organisation_id)
        if not internal_domains:
            return {
                "success": False,
                "error": "Cannot safely classify this attendee as external",
            }
        if is_internal_email(normalized_email, internal_domains):
            return {"success": False, "error": "Internal attendees cannot be enriched"}

        if not refresh:
            cached = await _find_cached_contact(context, normalized_email)
            if cached:
                return cached

        # Charge only real provider calls. Cached CRM reads above remain free and
        # a rejected rate limit never sends attendee data to Apollo.
        try:
            enforce_contact_research_rate_limit(str(context.user_id))
        except HTTPException as exc:
            return {
                "success": False,
                "error": "Contact research rate limit exceeded",
                "retry_after": exc.headers.get("Retry-After") if exc.headers else None,
            }
        result = await apollo_enrich_person(
            email=normalized_email,
            organization_name=company.strip() if company and company.strip() else None,
        )
    elif linkedin_url:
        if context and context.user_id:
            try:
                enforce_contact_research_rate_limit(str(context.user_id))
            except HTTPException as exc:
                return {
                    "success": False,
                    "error": "Contact research rate limit exceeded",
                    "retry_after": exc.headers.get("Retry-After") if exc.headers else None,
                }
        result = await apollo_enrich_person(linkedin_url=linkedin_url)
    else:
        return {
            "success": False,
            "error": "Provide an attendee email or a valid LinkedIn profile URL",
            "suggestion": "Use the email from the calendar invitation for meeting participants",
        }

    if not result.get("success"):
        return {
            "success": False,
            "error": str(result.get("error") or "Professional profile research failed"),
        }

    return {
        "success": True,
        "source": "apollo",
        "email": normalized_email,
        "linkedin_url": result.get("linkedin_url") or linkedin_url,
        "profile": _sanitized_apollo_profile(result),
        "enriched_at": None,
    }


def create_research_linkedin_profile(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Bind immutable tenant/user context to the read-only research tool."""

    async def research_linkedin_profile_tool(
        query: str | None = None,
        company: str | None = None,
        email: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Research an external person's professional background.

        For a meeting attendee, pass the exact invitation `email`; never search
        by display name. Existing CRM research is returned before Apollo is
        called. A validated LinkedIn profile URL may be passed in `query` for an
        explicit general research request.
        """
        logger.info(
            "[TOOL CALLED] research_linkedin_profile - mode=%s, refresh=%s",
            "email" if email else "linkedin_url",
            refresh,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ResearchLinkedInProfile",
            tool_func=research_linkedin_profile,
            parameters={
                "query": query,
                "company": company,
                "email": email,
                "context": context,
                "refresh": refresh,
            },
            description="Researching professional profile",
            fallback_value={"success": False, "error": "Tool execution failed"},
        )

        logger.info(
            "[TOOL RESULT] research_linkedin_profile success=%s source=%s",
            bool(result.get("success")),
            result.get("source"),
        )
        return dict(result) if result else {"success": False, "error": "No result"}

    return research_linkedin_profile_tool


__all__ = [
    "research_linkedin_profile",
    "create_research_linkedin_profile",
]
