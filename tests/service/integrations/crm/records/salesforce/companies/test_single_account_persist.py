from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.crm.records.salesforce.companies import company_sync_service
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

        async def fetch_pages(self):
            yield [
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

    async def fake_deactivate_missing(*, organisation_id, integration_id, seen_external_ids):
        return CompanyWriteResult()

    monkeypatch.setattr(service, "_load_context", fake_load_context)
    monkeypatch.setattr(service, "_persist", fake_persist)
    monkeypatch.setattr(service, "_deactivate_missing", fake_deactivate_missing)
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
        "Salesforce companies streaming persist started",
        "Salesforce companies chunk persist started",
        "Salesforce companies chunk persist completed",
        "Salesforce companies persist completed",
    ]
    assert company_log_records[1].chunk_size == 2000
    assert company_log_records[2].raw_companies == 2
    assert company_log_records[2].normalized_companies == 1
    assert company_log_records[3].companies_created == 1
    assert company_log_records[3].skipped_malformed == 1
    assert company_log_records[4].fetched_companies == 2
    assert company_log_records[4].companies_created == 1
    assert company_log_records[4].skipped_malformed == 1


@pytest.mark.asyncio
async def test_run_persists_accounts_in_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
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
    persist_calls: list[list[str]] = []
    deactivated_seen_ids: set[str] | None = None

    async def fake_load_context(*args, **kwargs):
        return SalesforceCompanySyncContext(
            token_info=token_info,
            instance_url="https://acme.my.salesforce.com",
        )

    class FakeAccountsFetcher:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def fetch_pages(self):
            yield [{"Id": "001A", "Name": "Acme"}]
            yield [{"Id": "001B", "Name": "Beta"}]
            yield [{"Id": "001C", "Name": "Core"}]

    async def fake_persist(
        *,
        organisation_id,
        integration_id,
        normalized,
        deactivate_missing,
    ):
        assert deactivate_missing is False
        persist_calls.append([company.external_id for company in normalized])
        return (
            CompanyWriteResult(companies_created=len(normalized)),
            {"rows_updated": 0, "per_op": {}},
        )

    async def fake_deactivate_missing(*, organisation_id, integration_id, seen_external_ids):
        nonlocal deactivated_seen_ids
        deactivated_seen_ids = set(seen_external_ids)
        return CompanyWriteResult()

    monkeypatch.setattr(service, "_load_context", fake_load_context)
    monkeypatch.setattr(service, "_persist", fake_persist)
    monkeypatch.setattr(service, "_deactivate_missing", fake_deactivate_missing)
    monkeypatch.setattr(company_sync_service, "_COMPANY_PERSIST_CHUNK_SIZE", 2)
    monkeypatch.setattr(
        "src.service.integrations.crm.records.salesforce.companies.company_sync_service.SalesforceAccountsFetcher",
        FakeAccountsFetcher,
    )

    result = await service.run(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )

    assert persist_calls == [["001A", "001B"], ["001C"]]
    assert deactivated_seen_ids == {"001A", "001B", "001C"}
    assert result.fetched_companies == 3
    assert result.companies_created == 3
