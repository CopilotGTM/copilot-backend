"""Regression tests for resolving CRM accounts from LinkedIn identity input."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.crm.accounts.contracts import AccountWriteResult
from src.service.crm.accounts.research_service import AccountProviderResearchResult
from src.service.crm.accounts.resolution_service import (
    AccountIdentityResolutionService,
    AccountIdentityResolveCommand,
)


class _FakeResult:
    def __init__(self, *, scalars=None):
        self._scalars = scalars or []

    def scalars(self):
        """Match SQLAlchemy's result API for service query tests."""
        return self

    def first(self):
        """Return the first fake scalar result like SQLAlchemy does."""
        return self._scalars[0] if self._scalars else None


class _ResolutionSession:
    def __init__(self, *, search_results: list[CRMCompany | None], company: CRMCompany):
        self._search_results = list(search_results)
        self.company = company
        self.commits = 0
        self.flushes = 0

    async def execute(self, _stmt):  # noqa: ANN001
        """Return queued account lookup results in resolver query order."""
        result = self._search_results.pop(0)
        return _FakeResult(scalars=[result] if result else [])

    async def get(self, _model, _item_id):  # noqa: ANN001
        """Return the account that provider persistence should update."""
        return self.company

    async def flush(self):
        """Record non-committing persistence flushes."""
        self.flushes += 1

    async def commit(self):
        """Record commits so resolver tests can prove it stays transaction-local."""
        self.commits += 1


@pytest.mark.asyncio
async def test_linkedin_resolves_existing_account_without_apollo_call() -> None:
    """Existing CRMCompany.linkedin_url wins before provider enrichment."""
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="Acme",
        linkedin_url="https://linkedin.com/company/acme",
    )
    session = _ResolutionSession(search_results=[company], company=company)

    async def provider_lookup(_payload):  # noqa: ANN001
        """Fail if resolver calls Apollo despite a local LinkedIn match."""
        raise AssertionError("Apollo should not run when LinkedIn already matches")

    result = await AccountIdentityResolutionService(
        session,
        org_id,
        provider_lookup=provider_lookup,
    ).resolve(AccountIdentityResolveCommand(linkedin_url=company.linkedin_url))

    assert result.status == "resolved"
    assert result.account is company
    assert result.created is False
    assert result.matched_by == "linkedin"


@pytest.mark.asyncio
async def test_linkedin_apollo_match_creates_account_and_persists_provider_data() -> None:
    """LinkedIn-only account input can create an account only after Apollo resolves it."""
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="acme.com",
        domain="acme.com",
        linkedin_url="https://linkedin.com/company/acme",
    )
    session = _ResolutionSession(search_results=[None], company=company)
    writer = SimpleNamespace(
        create_account=AsyncMock(
            return_value=AccountWriteResult(
                account=company,
                created=True,
                matched_by="created",
            )
        )
    )

    async def provider_lookup(_payload):  # noqa: ANN001
        """Return a usable Apollo organization for LinkedIn-only account resolution."""
        return AccountProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            name="Acme",
            domain="acme.com",
            website="https://acme.com",
            linkedin_url="https://linkedin.com/company/acme",
            company_enrichment={
                "provider": "apollo",
                "apollo_id": "org_123",
                "match_source": "linkedin_url",
            },
        )

    result = await AccountIdentityResolutionService(
        session,
        org_id,
        account_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(AccountIdentityResolveCommand(linkedin_url=company.linkedin_url))

    assert result.status == "resolved"
    assert result.account is company
    assert result.created is True
    assert result.matched_by == "apollo"
    writer.create_account.assert_awaited_once()
    assert company.name == "Acme"
    assert company.website == "https://acme.com"
    assert company.company_enrichment["apollo_id"] == "org_123"
    assert company.company_enrichment["match_source"] == "linkedin_url"
    assert session.flushes == 1
    assert session.commits == 0


@pytest.mark.asyncio
async def test_provider_canonical_linkedin_reuses_existing_account_without_domain() -> None:
    """Provider canonical LinkedIn should dedupe before name-only account creation."""
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="Acme",
        domain=None,
        linkedin_url="https://linkedin.com/company/acme-canonical",
    )
    session = _ResolutionSession(search_results=[None, company], company=company)
    writer = SimpleNamespace(
        create_account=AsyncMock(),
        find_accounts_by_domain=AsyncMock(return_value=[]),
    )

    async def provider_lookup(_payload):  # noqa: ANN001
        return AccountProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            name="Acme",
            domain=None,
            website=None,
            linkedin_url="https://linkedin.com/company/acme-canonical",
            company_enrichment={"provider": "apollo", "match_source": "linkedin_url"},
        )

    result = await AccountIdentityResolutionService(
        session,
        org_id,
        account_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(AccountIdentityResolveCommand(linkedin_url="https://linkedin.com/company/acme"))

    assert result.status == "resolved"
    assert result.account is company
    assert result.created is False
    assert result.matched_by == "linkedin"
    writer.create_account.assert_not_awaited()
    assert company.company_enrichment["provider"] == "apollo"
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_writer_only_match_reason_is_not_exposed_as_identity_match() -> None:
    """Identity resolution should not leak account-writer-only match reasons."""
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="Copied Account",
    )
    session = _ResolutionSession(search_results=[None], company=company)
    writer = SimpleNamespace(
        create_account=AsyncMock(
            return_value=AccountWriteResult(
                account=company,
                created=False,
                matched_by="copied_source",
            )
        )
    )

    async def provider_lookup(_payload):  # noqa: ANN001
        return AccountProviderResearchResult(status="not_found")

    result = await AccountIdentityResolutionService(
        session,
        org_id,
        account_writer=writer,
        provider_lookup=provider_lookup,
    ).resolve(AccountIdentityResolveCommand(name="Copied Account"))

    assert result.status == "resolved"
    assert result.account is company
    assert result.created is False
    assert result.matched_by is None


@pytest.mark.asyncio
async def test_linkedin_only_provider_error_uses_public_error() -> None:
    """Provider transport/config errors should stay hidden behind public wording."""
    org_id = uuid4()
    session = _ResolutionSession(search_results=[None], company=None)  # type: ignore[arg-type]

    async def provider_lookup(_payload):  # noqa: ANN001
        return AccountProviderResearchResult(
            status="error",
            error_detail="Apollo rate limit exceeded",
        )

    result = await AccountIdentityResolutionService(
        session,
        org_id,
        provider_lookup=provider_lookup,
    ).resolve(
        AccountIdentityResolveCommand(linkedin_url="https://linkedin.com/company/rate-limited")
    )

    assert result.status == "error"
    assert result.error_detail == "LinkedIn research could not resolve this account"
