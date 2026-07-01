"""Shared organisation internal-domain lookup for CRM services."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.infra.database.sessions import acquire_master_async_session
from src.service.crm.utils.identity import extract_email_domain


def normalize_internal_domains(domains_raw: Sequence[str | None] | None) -> set[str]:
    """Return canonical internal email domains from Organisation.domain values."""
    domains: set[str] = set()
    for domain in domains_raw or []:
        if not domain or not domain.strip():
            continue
        domain_lower = domain.strip().lower()
        domains.add(domain_lower)
        if domain_lower.startswith("www."):
            domains.add(domain_lower[4:])
    return domains


async def get_internal_domains(organisation_id: UUID) -> set[str]:
    """Load internal email domains for one organisation from the master schema.

    Organisation rows live in the shared master schema, so org-scoped sessions
    cannot read this field directly.
    """
    stmt = select(col(Organisation.domain)).where(
        col(Organisation.organisation_id) == organisation_id,
    )
    async for master_session in acquire_master_async_session():
        result = await master_session.execute(stmt)
    return normalize_internal_domains(result.scalar_one_or_none())


def is_internal_email(email: str | None, internal_domains: set[str]) -> bool:
    """Return whether an email belongs to one of the organisation domains."""
    domain = extract_email_domain(email)
    return bool(domain and domain in internal_domains)
