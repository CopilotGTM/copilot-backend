"""Account enrichment persistence and Apollo organization normalization."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.enrichment.apollo import apollo_enrich_organization

AccountProviderStatus = Literal["found", "not_found", "error"]
AccountSummaryStatus = Literal["generated", "skipped", "error"]
AccountProviderLookup = Callable[
    ["AccountProviderResearchInput"], Awaitable["AccountProviderResearchResult"]
]


@dataclass(frozen=True)
class AccountProviderResearchInput:
    """Provider lookup payload for one account identity."""

    organisation_id: UUID
    company_id: UUID
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    linkedin_url: str | None = None


@dataclass(frozen=True)
class AccountProviderResearchResult:
    """Structured provider result that can be persisted without an LLM."""

    status: AccountProviderStatus
    error_detail: str | None = None
    company_enrichment: dict[str, Any] | None = None
    enrichment_source: str | None = None
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    linkedin_url: str | None = None
    industry: str | None = None
    number_of_employees: int | None = None
    annual_revenue: float | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    address: str | None = None
    postal_code: str | None = None


@dataclass(frozen=True)
class AccountResearchSummaryResult:
    """No-tool summary result written to `CRMCompany.account_summary`."""

    status: AccountSummaryStatus
    error_detail: str | None = None
    summary_markdown: str | None = None


class AccountResearchService:
    """Persist account-level enrichment in one tenant-scoped session."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def persist_provider_result(
        self,
        company_id: UUID,
        result: AccountProviderResearchResult,
    ) -> bool:
        """Persist structured provider fields and mark completed attempts."""
        company = await self._load_company(company_id)
        if not company:
            return False

        now = datetime.now(UTC)
        if result.status == "not_found":
            company.last_researched_at = now
            await self.session.commit()
            return True

        if result.status != "found":
            return False

        company.last_researched_at = now
        company.company_enriched_at = now
        company.company_enrichment_source = result.enrichment_source or "apollo"
        company.company_enrichment = dict(result.company_enrichment or {})
        company.synced_at = now
        company.last_seen_at = now

        if self._should_fill_name(company.name, company.domain, result.name):
            company.name = result.name
        if self._should_fill_empty(company.domain, result.domain):
            company.domain = result.domain
        if self._should_fill_empty(company.website, result.website):
            company.website = result.website
        if self._should_fill_empty(company.linkedin_url, result.linkedin_url):
            company.linkedin_url = result.linkedin_url
        if self._should_fill_empty(company.industry, result.industry):
            company.industry = result.industry
        if company.number_of_employees is None and result.number_of_employees is not None:
            company.number_of_employees = result.number_of_employees
        if company.annual_revenue is None and result.annual_revenue is not None:
            company.annual_revenue = result.annual_revenue
        if self._should_fill_empty(company.phone, result.phone):
            company.phone = result.phone
        if self._should_fill_empty(company.city, result.city):
            company.city = result.city
        if self._should_fill_empty(company.state, result.state):
            company.state = result.state
        if self._should_fill_empty(company.country, result.country):
            company.country = result.country
        if self._should_fill_empty(company.address, result.address):
            company.address = result.address
        if self._should_fill_empty(company.postal_code, result.postal_code):
            company.postal_code = result.postal_code

        await self.session.commit()
        return True

    async def persist_summary(
        self,
        company_id: UUID,
        summary: AccountResearchSummaryResult,
    ) -> bool:
        """Persist generated account summary without changing structured enrichment."""
        if summary.status != "generated" or not summary.summary_markdown:
            return False

        company = await self._load_company(company_id)
        if not company:
            return False

        company.account_summary = summary.summary_markdown
        await self.session.commit()
        return True

    async def _load_company(self, company_id: UUID) -> CRMCompany | None:
        company = await self.session.get(CRMCompany, company_id)
        if not company or company.organisation_id != self.organisation_id or company.is_deleted:
            return None
        return company

    @staticmethod
    def _should_fill_empty(existing: Any, new: Any) -> bool:
        return new is not None and not existing

    @staticmethod
    def _should_fill_name(
        existing: str | None,
        domain: str | None,
        new: str | None,
    ) -> bool:
        if not new:
            return False
        if not existing:
            return True
        return bool(domain) and existing.strip().lower() == domain.strip().lower()


async def research_account_with_apollo(
    payload: AccountProviderResearchInput,
) -> AccountProviderResearchResult:
    """Run Apollo organization lookup and return provider fields."""
    result = await apollo_enrich_organization(
        domain=payload.domain,
        name=payload.name,
        website=payload.website,
        linkedin_url=payload.linkedin_url,
    )
    if not result.get("success"):
        error = str(result.get("error") or "Account provider research failed")
        status: AccountProviderStatus = "not_found" if "no matching" in error.lower() else "error"
        return AccountProviderResearchResult(status=status, error_detail=error)

    return AccountProviderResearchResult(
        status="found",
        enrichment_source="apollo",
        company_enrichment=_company_enrichment(result),
        name=result.get("name"),
        domain=result.get("domain"),
        website=result.get("website"),
        linkedin_url=result.get("linkedin_url"),
        industry=result.get("industry"),
        number_of_employees=_int_or_none(result.get("number_of_employees")),
        annual_revenue=_float_or_none(result.get("annual_revenue")),
        phone=result.get("phone"),
        city=result.get("city"),
        state=result.get("state"),
        country=result.get("country"),
        address=result.get("address"),
        postal_code=result.get("postal_code"),
    )


def _company_enrichment(profile: dict[str, Any]) -> dict[str, Any]:
    """Keep provider data compact and free of request/error metadata."""
    return {
        "provider": "apollo",
        "apollo_id": profile.get("apollo_id"),
        "name": profile.get("name"),
        "domain": profile.get("domain"),
        "website": profile.get("website"),
        "linkedin_url": profile.get("linkedin_url"),
        "industry": profile.get("industry"),
        "keywords": profile.get("keywords") or [],
        "technologies": profile.get("technologies") or [],
        "founded_year": profile.get("founded_year"),
        "logo_url": profile.get("logo_url"),
        "raw": profile.get("raw") or {},
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
