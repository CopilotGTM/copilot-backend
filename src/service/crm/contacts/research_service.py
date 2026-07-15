"""Contact research persistence and provider-result normalization.

Called by the contact research worker workflow after it has loaded a
tenant-owned contact and released the DB session for provider calls. This module
owns canonical writes to `CRMContact`; summary generation is a separate no-tool
agent step. Open deal stakeholder rows may keep old denormalized snapshots for
cheap list reads, but newly generated research markdown stays on the contact so
stakeholder reads can converge on contact-owned person research.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.utils.identity import normalize_email
from src.service.enrichment.apollo import apollo_enrich_person

logger = logging.getLogger(__name__)

ContactProviderStatus = Literal["found", "not_found", "error"]
ContactSummaryStatus = Literal["generated", "skipped", "error"]
ContactProviderLookup = Callable[
    ["ContactProviderResearchInput"], Awaitable["ContactProviderResearchResult"]
]

PROFILE_RAW_KEY = "_raw"
PROFILE_METADATA_KEY = "_metadata"
PROFILE_SCHEMA_VERSION = 2
CONTACT_RESEARCH_PROMPT_VERSION = "contact_career_v1"
# PostgreSQL JSONB can hold much larger values, but keeping one contact payload
# below 1 MiB prevents a provider anomaly from making routine contact reads expensive.
RAW_PAYLOAD_WARNING_BYTES = 256 * 1024
RAW_PAYLOAD_MAX_BYTES = 1024 * 1024
# Raw evidence plus the small normalized compatibility view must stay below
# this total JSONB size; otherwise the raw object is omitted before persistence.
PROFILE_ENRICHMENT_MAX_BYTES = 1280 * 1024
RESEARCH_FACT_PACK_MAX_BYTES = 50 * 1024
MAX_CONTACT_RESEARCH_CHARS = 4000
_UNSAFE_MARKDOWN_SCHEME = re.compile(r"(?:javascript|data|vbscript)\s*:", re.IGNORECASE)

_EMPLOYMENT_FACT_KEYS = (
    "title",
    "organization_name",
    "company",
    "start_date",
    "end_date",
    "current",
)
_EDUCATION_FACT_KEYS = (
    "school_name",
    "institution_name",
    "degree",
    "field_of_study",
    "start_date",
    "end_date",
)
_AFFILIATION_FACT_KEYS = (
    "name",
    "organization_name",
    "role",
    "title",
    "start_date",
    "end_date",
)
_CERTIFICATION_FACT_KEYS = (
    "name",
    "authority",
    "issuing_organization",
    "issue_date",
    "expiration_date",
    "credential_url",
)


class ContactResearchEvidenceError(ValueError):
    """Raised before invalid provider evidence can reach the model."""


class ContactResearchEvidenceTooLargeError(ContactResearchEvidenceError):
    """Raised before an oversized provider fact pack can reach the model."""


class ContactProfileEnrichmentTooLargeError(ValueError):
    """Raised when even normalized provider fields exceed the contact JSONB limit."""


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
            existing = dict(contact.profile_enrichment or {})
            metadata = dict(existing.get(PROFILE_METADATA_KEY) or {})
            metadata["last_attempt"] = {
                "status": "not_found",
                "attempted_at": now.isoformat(),
                "error_detail": result.error_detail,
            }
            if existing and existing.get("status") != "not_found":
                # A provider miss can be transient. Retain the last successful
                # profile so contact cards and agents do not regress to empty data.
                existing[PROFILE_METADATA_KEY] = metadata
                contact.profile_enrichment = existing
            else:
                contact.profile_enrichment = {
                    "provider": result.enrichment_source or "apollo",
                    "status": "not_found",
                    "error_detail": result.error_detail,
                    PROFILE_METADATA_KEY: metadata,
                }
                contact.contact_research = None
            if commit:
                await self.session.commit()
            else:
                await self.session.flush()
            return True

        if result.status != "found":
            return False

        previous_payload_hash = profile_payload_hash(contact.profile_enrichment)
        new_enrichment = dict(result.profile_enrichment or {})
        new_payload_hash = profile_payload_hash(new_enrichment)

        contact.profile_enriched_at = now
        contact.profile_enrichment_source = result.enrichment_source or "apollo"
        contact.profile_enrichment = new_enrichment

        if contact.contact_research and new_payload_hash != previous_payload_hash:
            # A summary is evidence-derived, so a changed provider object makes
            # the old markdown unsafe for cards and downstream stakeholder agents.
            contact.contact_research = None
            contact.last_researched_at = None

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
        *,
        prompt_version: str | None = None,
        source_payload_hash: str | None = None,
    ) -> bool:
        """Persist generated UI summary and the evidence version that produced it."""
        if summary.status != "generated" or not summary.summary_markdown:
            return False

        contact = await self._load_contact(contact_id)
        if not contact:
            return False

        markdown = summary.summary_markdown.strip()
        if not _is_safe_summary_markdown(markdown):
            logger.warning(
                "Contact research summary rejected because it violates storage bounds",
                extra={
                    "event": "contact_research_summary_rejected",
                    "contact_id": str(contact_id),
                    "summary_length": len(markdown),
                },
            )
            return False

        current_payload_hash = profile_payload_hash(contact.profile_enrichment)
        if source_payload_hash and current_payload_hash != source_payload_hash:
            # Provider refresh and summary generation use separate transactions.
            # Reject stale model output if another refresh won that race.
            logger.info(
                "Contact research summary skipped because provider evidence changed",
                extra={
                    "event": "contact_research_summary_stale",
                    "contact_id": str(contact_id),
                },
            )
            return False

        contact.contact_research = markdown
        contact.last_researched_at = datetime.now(UTC)
        if prompt_version or source_payload_hash:
            enrichment = dict(contact.profile_enrichment or {})
            metadata = dict(enrichment.get(PROFILE_METADATA_KEY) or {})
            if prompt_version:
                metadata["prompt_version"] = prompt_version
            if source_payload_hash:
                metadata["summary_source_hash"] = source_payload_hash
            enrichment[PROFILE_METADATA_KEY] = metadata
            contact.profile_enrichment = enrichment
        # Keep new research markdown contact-owned. Participant research remains
        # only a legacy fallback until all stakeholder reads join CRMContact.
        await self.session.commit()
        return True

    async def _load_contact(self, contact_id: UUID) -> CRMContact | None:
        """Lock one tenant-owned contact so concurrent research writes serialize."""
        stmt = (
            select(CRMContact)
            .where(
                col(CRMContact.contact_id) == contact_id,
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
            )
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

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
    try:
        profile_enrichment = _profile_enrichment(result, job_title)
    except ContactProfileEnrichmentTooLargeError:
        logger.warning(
            "Apollo person profile exceeded the safe contact enrichment limit",
            extra={"event": "contact_enrichment_profile_too_large"},
        )
        return ContactProviderResearchResult(
            status="error",
            error_detail="Provider profile exceeded the safe storage limit",
        )

    return ContactProviderResearchResult(
        status="found",
        profile_enrichment=profile_enrichment,
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
    """Build stable public fields while retaining a bounded provider evidence object."""
    enrichment: dict[str, Any] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
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
    raw = profile.get("raw")
    raw_payload, raw_metadata = _prepare_raw_payload(raw)
    if raw_payload is not None:
        enrichment[PROFILE_RAW_KEY] = raw_payload
    enrichment[PROFILE_METADATA_KEY] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "match_source": profile.get("match_source"),
        **raw_metadata,
    }
    if _json_size(enrichment) > PROFILE_ENRICHMENT_MAX_BYTES:
        enrichment.pop(PROFILE_RAW_KEY, None)
        metadata = enrichment[PROFILE_METADATA_KEY]
        if isinstance(metadata, dict):
            metadata["raw_omitted_reason"] = "combined_profile_too_large"
    if _json_size(enrichment) > PROFILE_ENRICHMENT_MAX_BYTES:
        raise ContactProfileEnrichmentTooLargeError(
            f"Normalized profile exceeds {PROFILE_ENRICHMENT_MAX_BYTES} bytes"
        )
    return enrichment


def public_profile_enrichment(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Return the stable enrichment contract without backend-only evidence keys."""
    return {key: value for key, value in (profile or {}).items() if not key.startswith("_")}


def profile_payload_hash(profile: dict[str, Any] | None) -> str | None:
    """Return the evidence hash used to reject stale generated research."""
    if not profile:
        return None
    metadata = profile.get(PROFILE_METADATA_KEY)
    if isinstance(metadata, dict):
        value = metadata.get("payload_hash")
        if isinstance(value, str):
            return value

    # Legacy and test-provider profiles may not contain a raw payload. Hash the
    # public evidence so prompt-version freshness still detects field changes.
    try:
        encoded = json.dumps(
            public_profile_enrichment(profile),
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def build_contact_research_fact_pack(profile: dict[str, Any]) -> dict[str, Any]:
    """Select career-complete provider facts for the summary agent.

    The full provider object stays durable in JSONB, while this bounded view
    keeps unrelated Apollo internals and verbose nested objects out of model input.
    """
    raw = profile.get(PROFILE_RAW_KEY)
    metadata = profile.get(PROFILE_METADATA_KEY)
    omission_reason = metadata.get("raw_omitted_reason") if isinstance(metadata, dict) else None
    if not isinstance(raw, dict) and omission_reason in {
        "payload_too_large",
        "combined_profile_too_large",
    }:
        # Apollo's normalized compatibility view contains at most five roles.
        # Refuse incomplete prose when the full career evidence was size-omitted.
        raise ContactResearchEvidenceTooLargeError(
            "Complete provider evidence was omitted by the storage size guard"
        )
    source = raw if isinstance(raw, dict) else public_profile_enrichment(profile)
    organization = (
        _selected_organization(source.get("organization"))
        if isinstance(raw, dict)
        else _select_non_empty(
            {
                "name": source.get("organization_name"),
                "primary_domain": source.get("organization_domain"),
            },
            ("name", "primary_domain"),
        )
    )

    fact_pack = {
        "person": _select_non_empty(
            source,
            (
                "id",
                "name",
                "first_name",
                "last_name",
                "title",
                "job_title",
                "headline",
                "linkedin_url",
                "city",
                "state",
                "country",
            ),
        ),
        "organization": organization,
        "employment_history": _records(source.get("employment_history"), _EMPLOYMENT_FACT_KEYS),
        "education": _records(
            source.get("education") or source.get("education_history"),
            _EDUCATION_FACT_KEYS,
        ),
        "affiliations": _records(
            source.get("affiliations")
            or source.get("memberships")
            or source.get("professional_affiliations"),
            _AFFILIATION_FACT_KEYS,
        ),
        "volunteer_experiences": _records(
            source.get("volunteer_experiences"),
            _AFFILIATION_FACT_KEYS,
        ),
        "certifications": _records(
            source.get("certifications"),
            _CERTIFICATION_FACT_KEYS,
        ),
    }
    fact_pack = {key: value for key, value in fact_pack.items() if value}
    try:
        encoded = json.dumps(
            fact_pack,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ContactResearchEvidenceError("Contact research evidence is not safe JSON") from exc
    if len(encoded) > RESEARCH_FACT_PACK_MAX_BYTES:
        # Do not truncate career history: an incomplete profile would violate
        # the user-facing contract. Keep the raw evidence and fail only summary generation.
        logger.warning(
            "Contact research fact pack exceeds recommended size",
            extra={"event": "contact_research_fact_pack_large", "payload_bytes": len(encoded)},
        )
        raise ContactResearchEvidenceTooLargeError(
            f"Contact research evidence exceeds {RESEARCH_FACT_PACK_MAX_BYTES} bytes"
        )
    return fact_pack


def _prepare_raw_payload(raw: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate and size one provider object without failing normalized enrichment."""
    if not isinstance(raw, dict):
        return None, {"raw_omitted_reason": "missing_or_invalid"}
    try:
        encoded = json.dumps(
            raw,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None, {"raw_omitted_reason": "not_json_serializable"}

    size = len(encoded)
    metadata = {
        "payload_bytes": size,
        "payload_hash": hashlib.sha256(encoded).hexdigest(),
    }
    if size > RAW_PAYLOAD_MAX_BYTES:
        logger.warning(
            "Apollo person payload omitted because it exceeds the safe contact-row limit",
            extra={"event": "contact_enrichment_raw_omitted", "payload_bytes": size},
        )
        return None, {**metadata, "raw_omitted_reason": "payload_too_large"}
    if size > RAW_PAYLOAD_WARNING_BYTES:
        logger.warning(
            "Apollo person payload is larger than expected",
            extra={"event": "contact_enrichment_raw_large", "payload_bytes": size},
        )
    return raw, metadata


def _json_size(value: Any) -> int:
    """Return canonical UTF-8 size for values already validated as provider JSON."""
    try:
        return len(
            json.dumps(
                value,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ContactProfileEnrichmentTooLargeError("Provider profile is not valid JSON") from exc


def _select_non_empty(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: value
        for key in keys
        if isinstance((value := source.get(key)), str | int | float | bool) and value != ""
    }


def _records(value: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        selected
        for record in value
        if isinstance(record, dict)
        if (selected := _select_non_empty(record, keys))
    ]


def _selected_organization(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    selected = _select_non_empty(
        value,
        ("id", "name", "primary_domain", "website_url", "linkedin_url", "industry"),
    )
    return selected or None


def _is_safe_summary_markdown(markdown: str) -> bool:
    """Reject active content while preserving headings and ordinary Markdown text."""
    if not markdown or len(markdown) > MAX_CONTACT_RESEARCH_CHARS:
        return False
    if any(ord(character) < 32 and character not in "\n\t" for character in markdown):
        return False
    # Contact cards only need headings, lists, and plain HTTPS links. Raw HTML,
    # images, and executable URI schemes add rendering risk without product value.
    return not (
        "<" in markdown
        or ">" in markdown
        or "![" in markdown
        or "](" in markdown
        or _UNSAFE_MARKDOWN_SCHEME.search(markdown)
    )


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
