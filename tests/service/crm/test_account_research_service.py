from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.crm.accounts.research_service import (
    AccountProviderResearchResult,
    AccountResearchService,
    AccountResearchSummaryResult,
)


@pytest.mark.asyncio
async def test_persist_provider_result_sets_account_enrichment_fields() -> None:
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="acme.com",
        domain="acme.com",
        synced_at=datetime(2026, 6, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=company), commit=AsyncMock())
    service = AccountResearchService(session, org_id)

    result = await service.persist_provider_result(
        company.company_id,
        AccountProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            company_enrichment={"provider": "apollo", "apollo_id": "abc"},
            name="Acme",
            website="https://acme.com",
            industry="Software",
            number_of_employees=250,
            annual_revenue=1000000,
            phone="+1-555-0100",
            city="San Francisco",
            state="CA",
            country="US",
        ),
    )

    assert result is True
    assert company.name == "Acme"
    assert company.website == "https://acme.com"
    assert company.industry == "Software"
    assert company.number_of_employees == 250
    assert company.annual_revenue == 1000000
    assert company.phone == "+1-555-0100"
    assert company.city == "San Francisco"
    assert company.state == "CA"
    assert company.country == "US"
    assert company.company_enrichment == {"provider": "apollo", "apollo_id": "abc"}
    assert company.company_enrichment_source == "apollo"
    assert company.company_enriched_at is not None
    assert company.last_researched_at is None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_provider_result_not_found_does_not_mark_summary_research() -> None:
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="Acme",
        domain="acme.com",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=company), commit=AsyncMock())
    service = AccountResearchService(session, org_id)

    result = await service.persist_provider_result(
        company.company_id,
        AccountProviderResearchResult(status="not_found", error_detail="No matching company"),
    )

    assert result is True
    assert company.last_researched_at is None
    assert company.company_enrichment == {}
    assert company.company_enrichment_source is None
    assert company.company_enriched_at is None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_summary_writes_account_summary_text() -> None:
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="Acme",
        domain="acme.com",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=company), commit=AsyncMock())
    service = AccountResearchService(session, org_id)

    result = await service.persist_summary(
        company.company_id,
        AccountResearchSummaryResult(
            status="generated",
            summary_markdown="- Acme sells workflow software to mid-market teams.",
        ),
    )

    assert result is True
    assert company.account_summary == "- Acme sells workflow software to mid-market teams."
    assert company.last_researched_at is not None
    session.commit.assert_awaited_once()
