from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.contacts import contact_sync_service
from src.service.integrations.crm.records.salesforce.contacts.contact_sync_service import (
    SalesforceContactRecordSyncService,
    SalesforceContactSyncContext,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_types import (
    ContactWriteResult,
    NormalizedSalesforceContact,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo


@pytest.mark.asyncio
async def test_persist_single_contact_reuses_contact_writer_and_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceContactRecordSyncService()
    captured: dict[str, object] = {}

    async def fake_persist(
        *,
        organisation_id,
        integration_id,
        normalized,
        backfill_deals=True,
    ):
        assert backfill_deals is True
        captured["normalized"] = normalized
        return (
            ContactWriteResult(contacts_updated=1, contacts_reactivated=1),
            {"rows_updated": 1, "per_op": {"primary_contact_external_id": 1}},
        )

    monkeypatch.setattr(service, "_persist", fake_persist)

    result = await service.persist_single_contact(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        raw_contact={"Id": "003xx0000000001AAA", "Email": "alex@example.com"},
    )

    assert result["contacts_updated"] == 1
    assert result["contacts_reactivated"] == 1
    assert result["deal_backfill_rows_updated"] == 1


@pytest.mark.asyncio
async def test_run_persists_contacts_in_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SalesforceContactRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    persist_calls: list[list[str]] = []
    bulk_backfill_calls = 0

    class FakeContactsFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def fetch_pages(self):
            yield [
                {"Id": "003A", "Email": "a@example.com"},
                {"Id": "003B", "Email": "b@example.com"},
            ]
            yield [{"Id": "003C", "Email": "c@example.com"}]

    async def fake_load_context(*_args, **_kwargs):
        return SalesforceContactSyncContext(
            token_info=IntegrationTokenInfo(
                integration_id=integration_id,
                organisation_id=organisation_id,
                access_token="token",
                refresh_token=None,
                access_token_expires_at=None,
            ),
            instance_url="https://example.my.salesforce.com",
        )

    async def fake_persist(*, organisation_id, integration_id, normalized, backfill_deals=True):
        assert backfill_deals is False
        persist_calls.append([contact.external_id for contact in normalized])
        return (
            ContactWriteResult(
                contacts_created=len(normalized),
                unresolved_owner_link=1 if len(persist_calls) == 2 else 0,
            ),
            {"rows_updated": 0, "per_op": {}},
        )

    async def fake_backfill_deal_primary_contacts(**_kwargs):
        nonlocal bulk_backfill_calls
        bulk_backfill_calls += 1
        return {"rows_updated": 2, "per_op": {"primary_contact_id": 2}}

    monkeypatch.setattr(contact_sync_service, "_CONTACT_PERSIST_CHUNK_SIZE", 2)
    monkeypatch.setattr(contact_sync_service, "SalesforceContactsFetcher", FakeContactsFetcher)
    monkeypatch.setattr(service, "_load_context", fake_load_context)
    monkeypatch.setattr(service, "_persist", fake_persist)
    monkeypatch.setattr(
        service,
        "_backfill_deal_primary_contacts",
        fake_backfill_deal_primary_contacts,
    )

    result = await service.run(organisation_id=organisation_id, integration_id=integration_id)

    assert persist_calls == [["003A", "003B"], ["003C"]]
    assert bulk_backfill_calls == 1
    assert result.fetched_contacts == 3
    assert result.contacts_created == 3
    assert result.unresolved_owner_link == 1
    assert result.deal_backfill_rows_updated == 2


@pytest.mark.asyncio
async def test_persist_loads_company_and_owner_maps_for_current_chunk_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceContactRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    company_lookup_ids: list[set[str]] = []
    owner_lookup_ids: list[set[str]] = []

    class FakeSession:
        async def commit(self) -> None:
            pass

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    async def fake_load_company_map(**kwargs):
        company_lookup_ids.append(set(kwargs["external_ids"]))
        return {"001A": uuid4()}

    async def fake_load_owner_map(**kwargs):
        owner_lookup_ids.append(set(kwargs["external_ids"]))
        return {"005A": uuid4()}

    class FakeWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, *, normalized, company_map, owner_map):
            assert set(company_map) == {"001A"}
            assert set(owner_map) == {"005A"}
            return ContactWriteResult(contacts_created=len(normalized))

    async def fake_load_contact_pk_map(**_kwargs):
        return {}

    monkeypatch.setattr(contact_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(service, "_load_company_map", fake_load_company_map)
    monkeypatch.setattr(service, "_load_owner_map", fake_load_owner_map)
    monkeypatch.setattr(service, "_load_contact_pk_map", fake_load_contact_pk_map)
    monkeypatch.setattr(contact_sync_service, "SalesforceContactWriter", FakeWriter)

    normalized = [
        _normalized_contact("003A", company_external_id="001A", owner_external_id="005A"),
        _normalized_contact("003B", company_external_id="001A", owner_external_id="005A"),
        _normalized_contact("003C", company_external_id=None, owner_external_id=None),
    ]

    write_result, backfill_payload = await service._persist(
        organisation_id=organisation_id,
        integration_id=integration_id,
        normalized=normalized,
        backfill_deals=False,
    )

    assert write_result.contacts_created == 3
    assert backfill_payload == {"rows_updated": 0, "per_op": {}}
    assert company_lookup_ids == [{"001A"}]
    assert owner_lookup_ids == [{"005A"}]


@pytest.mark.asyncio
async def test_deal_primary_contact_backfill_pages_referenced_contacts_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceContactRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    committed = 0
    deal_reference_calls: list[str | None] = []
    contact_lookup_ids: list[list[str]] = []
    backfill_mappings: list[list[str]] = []

    class FakeSession:
        async def commit(self) -> None:
            nonlocal committed
            committed += 1

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    async def fake_load_deal_refs(**kwargs):
        after_external_id = kwargs["after_external_id"]
        deal_reference_calls.append(after_external_id)
        if after_external_id is None:
            return ["003A", "003B"]
        if after_external_id == "003B":
            return ["003C"]
        return []

    async def fake_load_contact_backfill_map(**kwargs):
        external_ids = list(kwargs["external_ids"])
        contact_lookup_ids.append(external_ids)
        return {
            external_id: (uuid4(), external_id, None, f"{external_id.lower()}@example.com")
            for external_id in external_ids
            if external_id != "003B"
        }

    async def fake_backfill_crm_deals(_session, *, organisation_id, integration_id, ops):
        mapping = dict(ops[0].mapping)
        backfill_mappings.append(list(mapping))

        class Result:
            rows_updated = len(mapping)
            per_op = {"primary_contact_id": len(mapping)}

        return Result()

    monkeypatch.setattr(contact_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(service, "_load_deal_primary_contact_external_ids", fake_load_deal_refs)
    monkeypatch.setattr(service, "_load_contact_backfill_map", fake_load_contact_backfill_map)
    monkeypatch.setattr(contact_sync_service, "backfill_crm_deals", fake_backfill_crm_deals)

    result = await service._backfill_deal_primary_contacts(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )

    assert deal_reference_calls == [None, "003B", "003C"]
    assert contact_lookup_ids == [["003A", "003B"], ["003C"]]
    assert backfill_mappings == [["003A"], ["003C"]]
    assert committed == 2
    assert result == {"rows_updated": 2, "per_op": {"primary_contact_id": 2}}


def _normalized_contact(
    external_id: str,
    *,
    company_external_id: str | None,
    owner_external_id: str | None,
) -> NormalizedSalesforceContact:
    return NormalizedSalesforceContact(
        external_id=external_id,
        first_name=None,
        last_name=None,
        full_name=external_id,
        job_title=None,
        company_name=None,
        company_external_id=company_external_id,
        email=None,
        additional_emails=(),
        phone=None,
        mobile_phone=None,
        fax=None,
        website=None,
        owner_external_id=owner_external_id,
        lifecycle_stage=None,
        lead_status=None,
        marketing_status=None,
        address=None,
        city=None,
        state=None,
        postal_code=None,
        country=None,
        timezone=None,
        last_contacted_at=None,
        last_activity_at=None,
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={},
    )
