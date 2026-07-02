from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_insight_services.account_research_service as workflow_module
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.crm.accounts.research_service import (
    AccountProviderResearchInput,
    AccountProviderResearchResult,
    AccountResearchSummaryResult,
)
from src.workers.deal_worker.services.deal_insight_services.account_research_service import (
    AccountResearchWorkflowService,
)


@pytest.mark.asyncio
async def test_account_research_workflow_generates_summary_from_existing_enrichment(
    monkeypatch,
) -> None:
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="Acme",
        domain="acme.com",
        company_enrichment={"provider": "apollo", "name": "Acme", "industry": "Software"},
    )
    sessions = iter(
        [
            SimpleNamespace(get=AsyncMock(return_value=company)),
            SimpleNamespace(get=AsyncMock(return_value=company), commit=AsyncMock()),
            SimpleNamespace(get=AsyncMock(return_value=company), commit=AsyncMock()),
        ]
    )

    async def fake_acquire_session(_organisation_id):
        yield next(sessions)

    async def provider_should_not_run(_payload: AccountProviderResearchInput):
        raise AssertionError("existing company enrichment should skip Apollo")

    async def fake_summary(enrichment: dict):
        assert enrichment["name"] == "Acme"
        return AccountResearchSummaryResult(
            status="generated",
            summary_markdown="- Acme is a software company relevant for sales outreach.",
        )

    monkeypatch.setattr(workflow_module, "acquire_org_async_session", fake_acquire_session)
    service = AccountResearchWorkflowService(
        org_id,
        provider_lookup=provider_should_not_run,
        summary_generator=fake_summary,
    )

    result = await service.research_account(company.company_id)

    assert result.provider_status == "cached"
    assert result.summary_status == "generated"
    assert company.account_summary == ("- Acme is a software company relevant for sales outreach.")


@pytest.mark.asyncio
async def test_account_research_workflow_generates_summary_after_provider_data(
    monkeypatch,
) -> None:
    org_id = uuid4()
    company = CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=uuid4(),
        name="acme.com",
        domain="acme.com",
    )
    sessions = iter(
        [
            SimpleNamespace(get=AsyncMock(return_value=company)),
            SimpleNamespace(get=AsyncMock(return_value=company), commit=AsyncMock()),
            SimpleNamespace(get=AsyncMock(return_value=company), commit=AsyncMock()),
        ]
    )

    async def fake_acquire_session(_organisation_id):
        yield next(sessions)

    async def fake_provider(_payload: AccountProviderResearchInput):
        return AccountProviderResearchResult(
            status="found",
            enrichment_source="apollo",
            company_enrichment={"provider": "apollo", "name": "Acme"},
            name="Acme",
            industry="Software",
        )

    async def fake_summary(_enrichment: dict):
        return AccountResearchSummaryResult(
            status="generated",
            summary_markdown="- Acme is a software company relevant for sales outreach.",
        )

    monkeypatch.setattr(workflow_module, "acquire_org_async_session", fake_acquire_session)
    service = AccountResearchWorkflowService(
        org_id,
        provider_lookup=fake_provider,
        summary_generator=fake_summary,
    )

    result = await service.research_account(company.company_id)

    assert result.provider_status == "found"
    assert result.summary_status == "generated"
    assert company.company_enrichment == {"provider": "apollo", "name": "Acme"}
    assert company.account_summary == ("- Acme is a software company relevant for sales outreach.")
