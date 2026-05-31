from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.contacts.contact_sync_service import (
    SalesforceContactRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_types import (
    ContactWriteResult,
)


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
