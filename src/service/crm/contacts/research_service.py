"""Contact research persistence and provider-result normalization.

Called by the contact research worker workflow after it has loaded a
tenant-owned contact and released the DB session for provider calls. This module
owns canonical writes to `CRMContact`; summary generation is a separate no-tool
agent step. Open deal stakeholder rows may keep old denormalized snapshots for
cheap list reads, but newly generated research markdown stays on the contact so
stakeholder reads can converge on contact-owned person research.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.utils.identity import normalize_email
from src.service.enrichment.apollo import apollo_enrich_person

ContactProviderStatus = Literal["found", "not_found", "error"]
ContactSummaryStatus = Literal["generated", "skipped", "error"]
ContactProviderLookup = Callable[
    ["ContactProviderResearchInput"], Awaitable["ContactProviderResearchResult"]
]


@dataclass(frozen=True)
class ContactProviderResearchInput:
    """Provider lookup payload for one contact identity."""

    organisation_id: UUID
    contact_id: UUID
    email: str | None = None
    name: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    linkedin_url: str | None = None


@dataclass(frozen=True)
class ContactProviderResearchResult:
    """Structured provider result that can be persisted without an LLM."""

    status: ContactProviderStatus
    error_detail: str | None = None
    profile_enrichment: dict[str, Any] | None = None
    enrichment_source: str | None = None
    email: str | None = None
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None
    company_domain: str | None = None


@dataclass(frozen=True)
class ContactResearchSummaryResult:
    """No-tool summary result written to `CRMContact.contact_research`."""

    status: ContactSummaryStatus
    error_detail: str | None = None
    summary_markdown: str | None = None


class ContactResearchService:
    """Persist contact-level enrichment in one tenant-scoped session."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def persist_provider_result(
        self,
        contact_id: UUID,
        result: ContactProviderResearchResult,
        *,
        commit: bool = True,
    ) -> bool:
        """Persist structured provider fields and mark successful research attempts.

        The contact remains the durable person record. Stakeholder list/detail
        reads join contact data, so provider profile fields are not mirrored
        back into participant snapshots.
        """
        contact = await self._load_contact(contact_id)
        if not contact:
            return False

        now = datetime.now(UTC)
        if result.status == "not_found":
            contact.profile_enriched_at = now
            contact.profile_enrichment_source = result.enrichment_source or "apollo"
            contact.profile_enrichment = {
                "provider": result.enrichment_source or "apollo",
                "status": "not_found",
                "error_detail": result.error_detail,
            }
            contact.contact_research = None
            await self.session.commit()
            return True

        if result.status != "found":
            return False

        contact.profile_enriched_at = now
        contact.profile_enrichment_source = result.enrichment_source or "apollo"
        contact.profile_enrichment = dict(result.profile_enrichment or {})

        if self._should_fill(contact.full_name, result.name):
            contact.full_name = result.name
        if self._should_fill(contact.first_name, result.first_name):
            contact.first_name = result.first_name
        if self._should_fill(contact.last_name, result.last_name):
            contact.last_name = result.last_name
        if self._should_fill_empty(contact.email, result.email):
            contact.email = normalize_email(result.email)
        if self._should_fill_empty(contact.job_title, result.job_title):
            contact.job_title = result.job_title
        if self._should_fill(contact.linkedin_url, result.linkedin_url):
            contact.linkedin_url = result.linkedin_url

        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
            await self.session.refresh(contact)
        return True

    async def persist_summary(
        self,
        contact_id: UUID,
        summary: ContactResearchSummaryResult,
    ) -> bool:
        """Persist generated UI summary without changing structured enrichment."""
        if summary.status != "generated" or not summary.summary_markdown:
            return False

        contact = await self._load_contact(contact_id)
        if not contact:
            return False

        contact.contact_research = summary.summary_markdown
        contact.last_researched_at = datetime.now(UTC)
        # Keep new research markdown contact-owned. Participant research remains
        # only a legacy fallback until all stakeholder reads join CRMContact.
        await self.session.commit()
        return True

    async def _load_contact(self, contact_id: UUID) -> CRMContact | None:
        """Load a tenant-owned, non-deleted contact before writing research fields."""
        contact = await self.session.get(CRMContact, contact_id)
        if not contact or contact.organisation_id != self.organisation_id or contact.is_deleted:
            return None
        return contact

    @staticmethod
    def _should_fill(existing: str | None, new: str | None) -> bool:
        if not new:
            return False
        if not existing:
            return True
        return len(new) > len(existing)

    @staticmethod
    def _should_fill_empty(existing: str | None, new: str | None) -> bool:
        return bool(new) and not existing


async def research_contact_with_apollo(
    payload: ContactProviderResearchInput,
) -> ContactProviderResearchResult:
    """Run Apollo lookup and return provider fields without generating prose."""
    result = await apollo_enrich_person(
        name=payload.name,
        email=payload.email,
        organization_name=payload.company_name,
        domain=payload.company_domain,
        linkedin_url=payload.linkedin_url,
    )
    if not result.get("success"):
        error = str(result.get("error") or "Contact provider research failed")
        status: ContactProviderStatus = (
            "not_found" if "no matching person" in error.lower() else "error"
        )
        return ContactProviderResearchResult(status=status, error_detail=error)

    job_title = _resolve_job_title(result)
    result["match_source"] = _match_source(payload)
    return ContactProviderResearchResult(
        status="found",
        profile_enrichment=_profile_enrichment(result, job_title),
        enrichment_source="apollo",
        email=result.get("email"),
        name=result.get("name"),
        first_name=result.get("first_name"),
        last_name=result.get("last_name"),
        job_title=job_title,
        linkedin_url=result.get("linkedin_url"),
        company_name=result.get("organization_name"),
        company_domain=result.get("organization_domain"),
    )


def _profile_enrichment(profile: dict[str, Any], job_title: str | None) -> dict[str, Any]:
    """Keep provider data compact and free of request/error metadata."""
    return {
        "provider": "apollo",
        "apollo_id": profile.get("apollo_id"),
        "match_source": profile.get("match_source"),
        "email": profile.get("email"),
        "name": profile.get("name"),
        "first_name": profile.get("first_name"),
        "last_name": profile.get("last_name"),
        "job_title": job_title,
        "headline": profile.get("headline"),
        "linkedin_url": profile.get("linkedin_url"),
        "photo_url": profile.get("photo_url"),
        "location": profile.get("location"),
        "organization_name": profile.get("organization_name"),
        "organization_domain": profile.get("organization_domain"),
        "employment_history": profile.get("employment_history") or [],
    }


def _resolve_job_title(profile: dict[str, Any]) -> str | None:
    """Pick a structured current role without treating profile headline as a title."""
    title = profile.get("title") or profile.get("job_title")
    if title:
        return str(title)

    employment_history = profile.get("employment_history") or []
    if not isinstance(employment_history, list):
        return None

    for role in employment_history:
        if not isinstance(role, dict) or not _is_current_role(role):
            continue
        current_title = role.get("title")
        if current_title:
            return str(current_title)

    return None


def _match_source(payload: ContactProviderResearchInput) -> str | None:
    if payload.linkedin_url:
        return "linkedin_url"
    if payload.email:
        return "email"
    if payload.name:
        return "name"
    return None


def _is_current_role(role: dict[str, Any]) -> bool:
    if role.get("current") is True:
        return True

    end_date = role.get("end_date")
    if end_date is None:
        return True
    return isinstance(end_date, str) and end_date.strip().lower() in {
        "",
        "present",
        "current",
        "now",
    }
