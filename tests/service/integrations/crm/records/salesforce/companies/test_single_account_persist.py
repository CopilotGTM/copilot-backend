from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.crm.records.salesforce.companies.company_sync_service import (
    SalesforceCompanyRecordSyncService,
    SalesforceCompanySyncContext,
)
from src.service.integrations.crm.records.salesforce.companies.company_types import (
    CompanyWriteResult,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo


@pytest.mark.asyncio
async def test_persist_single_account_reuses_company_writer_without_snapshot_deactivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceCompanyRecordSyncService()
    captured: dict[str, object] = {}

    async def fake_persist(
        *,
        organisation_id,
        integration_id,
        normalized,
        deactivate_missing,
    ):
        captured["normalized"] = normalized
        captured["deactivate_missing"] = deactivate_missing
        return (
            CompanyWriteResult(companies_created=1),
            {"rows_updated": 2, "per_op": {"company_external_id": 2}},
        )

    monkeypatch.setattr(service, "_persist", fake_persist)

    result = await service.persist_single_account(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        raw_account={"Id": "001xx0000000001AAA", "Name": "Acme"},
    )

    assert result["companies_created"] == 1
    assert result["deal_backfill_rows_updated"] == 2
    assert captured["deactivate_missing"] is False


@pytest.mark.asyncio
async def test_run_logs_company_sync_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = SalesforceCompanyRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    token_info = IntegrationTokenInfo(
        access_token="access-token",
        refresh_token="refresh-token",
        integration_type=IntegrationType.SALESFORCE,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )

    async def fake_load_context(*args, **kwargs):
        return SalesforceCompanySyncContext(
            token_info=token_info,
            instance_url="https://acme.my.salesforce.com",
        )

    class FakeAccountsFetcher:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def fetch_all(self) -> list[dict[str, str]]:
            return [
                {"Id": "001A", "Name": "Acme"},
                {"Name": "Missing Id"},
            ]

    async def fake_persist(
        *,
        organisation_id,
        integration_id,
        normalized,
        deactivate_missing,
    ):
        return (
            CompanyWriteResult(companies_created=len(normalized)),
            {"rows_updated": 0, "per_op": {}},
        )

    monkeypatch.setattr(service, "_load_context", fake_load_context)
    monkeypatch.setattr(service, "_persist", fake_persist)
    monkeypatch.setattr(
        "src.service.integrations.crm.records.salesforce.companies.company_sync_service.SalesforceAccountsFetcher",
        FakeAccountsFetcher,
    )

    with caplog.at_level(logging.INFO):
        result = await service.run(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )

    assert result.fetched_companies == 2
    company_log_records = [
        record
        for record in caplog.records
        if getattr(record, "integration_id", None) == str(integration_id)
    ]
    assert [record.message for record in company_log_records] == [
        "Salesforce companies sync started",
        "Salesforce companies fetch completed",
        "Salesforce companies normalize completed",
        "Salesforce companies persist started",
        "Salesforce companies persist completed",
    ]
    assert company_log_records[1].fetched_companies == 2
    assert company_log_records[2].normalized_companies == 1
    assert company_log_records[2].skipped_malformed == 1
    assert company_log_records[4].companies_created == 1
