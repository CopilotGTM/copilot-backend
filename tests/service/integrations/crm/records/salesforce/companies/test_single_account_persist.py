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
    NormalizedSalesforceCompany,
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


@pytest.mark.asyncio
async def test_persist_loads_owner_map_for_current_companies_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceCompanyRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    owner_lookup_ids: list[set[str]] = []

    class FakeSession:
        async def commit(self) -> None:
            pass

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    async def fake_load_owner_map(**kwargs):
        owner_lookup_ids.append(set(kwargs["external_ids"]))
        return {"005A": uuid4()}

    class FakeWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, *, normalized, all_external_ids, owner_map, deactivate_missing):
            assert all_external_ids == ["001A", "001B", "001C"]
            assert set(owner_map) == {"005A"}
            assert deactivate_missing is False
            return CompanyWriteResult(companies_created=len(normalized))

    async def fake_load_external_to_pk(**_kwargs):
        return {}

    monkeypatch.setattr(company_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(company_sync_service, "SalesforceCompanyWriter", FakeWriter)
    monkeypatch.setattr(service, "_load_owner_map", fake_load_owner_map)
    monkeypatch.setattr(service, "_load_external_to_pk", fake_load_external_to_pk)

    normalized = [
        _normalized_company("001A", owner_external_id="005A"),
        _normalized_company("001B", owner_external_id="005A"),
        _normalized_company("001C", owner_external_id=None),
    ]

    write_result, backfill_payload = await service._persist(
        organisation_id=organisation_id,
        integration_id=integration_id,
        normalized=normalized,
        deactivate_missing=False,
    )

    assert write_result.companies_created == 3
    assert backfill_payload == {"rows_updated": 0, "per_op": {}}
    assert owner_lookup_ids == [{"005A"}]


def _normalized_company(
    external_id: str,
    *,
    owner_external_id: str | None,
) -> NormalizedSalesforceCompany:
    return NormalizedSalesforceCompany(
        external_id=external_id,
        name=external_id,
        domain=None,
        website=None,
        description=None,
        industry=None,
        company_type=None,
        lifecycle_stage=None,
        owner_external_id=owner_external_id,
        number_of_employees=None,
        annual_revenue=None,
        phone=None,
        address=None,
        city=None,
        state=None,
        postal_code=None,
        country=None,
        timezone=None,
        linkedin_url=None,
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={},
    )
