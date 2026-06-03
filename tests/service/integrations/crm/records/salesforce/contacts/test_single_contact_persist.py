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
    ):
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

    async def fake_persist(*, organisation_id, integration_id, normalized):
        persist_calls.append([contact.external_id for contact in normalized])
        return (
            ContactWriteResult(
                contacts_created=len(normalized),
                unresolved_owner_link=1 if len(persist_calls) == 2 else 0,
            ),
            {"rows_updated": len(normalized), "per_op": {"primary_contact_external_id": 1}},
        )

    monkeypatch.setattr(contact_sync_service, "_CONTACT_PERSIST_CHUNK_SIZE", 2)
    monkeypatch.setattr(contact_sync_service, "SalesforceContactsFetcher", FakeContactsFetcher)
    monkeypatch.setattr(service, "_load_context", fake_load_context)
    monkeypatch.setattr(service, "_persist", fake_persist)

    result = await service.run(organisation_id=organisation_id, integration_id=integration_id)

    assert persist_calls == [["003A", "003B"], ["003C"]]
    assert result.fetched_contacts == 3
    assert result.contacts_created == 3
    assert result.unresolved_owner_link == 1
    assert result.deal_backfill_rows_updated == 3
