"""Internal-user guardrail for stakeholder upsert.

Prevents internal team members (our own employees) from being added as
deal stakeholders. Stakeholders must be external/client-side contacts only.

Two-tier domain detection:
1. Organisation.domain — authoritative source (admin-configured)
2. If empty, derive from CRMDealParticipant emails where participant_type=USER
   (free email providers like gmail/hotmail are excluded from derived domains,
    but the exact participant emails are still kept for matching)

Results are cached in context.metadata so the DB is hit only once per agent run.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.infra.database.sessions import acquire_master_async_session

if TYPE_CHECKING:
    from src.agents.context import AgentContext

logger = logging.getLogger(__name__)

# Metadata keys used for caching
_KEY_LOADED = "_internal_guard_loaded"
_KEY_DOMAINS = "_internal_domains"
_KEY_EMAILS = "_internal_emails"

# Free email providers — excluded when deriving org domains from participant emails
FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "ymail.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "zoho.com",
        "mail.com",
        "gmx.com",
        "gmx.net",
    }
)


async def _load_internal_user_data(
    context: AgentContext,
) -> tuple[set[str], set[str]]:
    """Load internal domains and emails for the current deal.

    Two-tier approach:
    1. Organisation.domain (authoritative, admin-configured)
    2. If empty, fall back to USER participant emails to derive domains

    When using the participant fallback, free email providers (gmail, hotmail,
    etc.) are excluded from the domain set but exact participant emails are
    kept so those specific people are still blocked.

    Returns:
        (internal_domains, internal_emails) — all lowercased
    """
    internal_domains: set[str] = set()
    internal_emails: set[str] = set()

    # Tier 1: Organisation.domain from public schema (requires master session)
    try:
        async for master_session in acquire_master_async_session():
            org_stmt = select(col(Organisation.domain)).where(
                col(Organisation.organisation_id) == context.organisation_id,
            )
            org_result = await master_session.execute(org_stmt)
            domains_raw = org_result.scalar_one_or_none()
            if domains_raw and isinstance(domains_raw, list):
                for d in domains_raw:
                    if not d or not d.strip():
                        continue
                    d_lower = d.strip().lower()
                    internal_domains.add(d_lower)
                    if d_lower.startswith("www."):
                        internal_domains.add(d_lower[4:])
    except SQLAlchemyError:
        logger.warning("Failed to load org domains from public schema", exc_info=True)

    async with context.acquire_session() as session:
        # Tier 2: If no org domains found, derive from USER participant emails
        if not internal_domains and context.deal_id:
            try:
                participant_stmt = select(col(CRMDealParticipant.related_email)).where(
                    col(CRMDealParticipant.deal_id) == context.deal_id,
                    col(CRMDealParticipant.participant_type) == CRMParticipantType.USER,
                )
                participant_result = await session.execute(participant_stmt)
                for (email,) in participant_result.all():
                    if not email or not isinstance(email, str) or not email.strip():
                        continue
                    email_lower = email.strip().lower()
                    # Always keep exact email so this specific person is blocked
                    internal_emails.add(email_lower)
                    # Derive domain, but skip free providers
                    if "@" in email_lower:
                        domain = email_lower.split("@")[-1]
                        if domain not in FREE_EMAIL_DOMAINS:
                            internal_domains.add(domain)
            except SQLAlchemyError:
                with contextlib.suppress(Exception):
                    await session.rollback()

    return internal_domains, internal_emails


async def _ensure_cache(context: AgentContext) -> None:
    """Ensure internal-user data is loaded and cached in context.metadata."""
    if context.metadata.get(_KEY_LOADED):
        return

    domains, emails = await _load_internal_user_data(context)
    context.metadata[_KEY_DOMAINS] = domains
    context.metadata[_KEY_EMAILS] = emails
    context.metadata[_KEY_LOADED] = True


async def is_internal_email(context: AgentContext, email: str) -> bool:
    """Check whether an email belongs to an internal team member.

    Public API for the guardrail. Loads and caches data on first call.

    Args:
        context: Agent context with organisation_id and deal_id
        email: Email address to check

    Returns:
        True if the email is internal (should be rejected as stakeholder)
    """
    await _ensure_cache(context)

    domains: set[str] = context.metadata.get(_KEY_DOMAINS, set())
    emails: set[str] = context.metadata.get(_KEY_EMAILS, set())

    email_lower = email.strip().lower()
    if not email_lower:
        return False

    # Exact email match (participant fallback)
    if email_lower in emails:
        return True

    # Domain match
    if "@" in email_lower:
        domain = email_lower.split("@")[-1]
        if domain in domains:
            return True

    return False
