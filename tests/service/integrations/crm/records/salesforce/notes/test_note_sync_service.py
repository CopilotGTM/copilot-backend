from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.notes import note_sync_service
from src.service.integrations.crm.records.salesforce.notes.note_sync_service import (
    SalesforceNoteRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    NormalizedSalesforceNote,
    NoteWriteResult,
)


def _normalized_note(
    external_id: str = "069xx0000000001AAA",
    *,
    owner_external_id: str | None = None,
    deal_external_id: str | None = "006xx0000000001AAA",
    contact_external_id: str | None = None,
    company_external_id: str | None = None,
) -> NormalizedSalesforceNote:
    return NormalizedSalesforceNote(
        external_id=external_id,
        subject="QBR summary",
        body="Talked about renewal.",
        activity_subtype="SALESFORCE_NOTE",
        owner_external_id=owner_external_id,
        deal_external_id=deal_external_id,
        contact_external_id=contact_external_id,
        company_external_id=company_external_id,
        related_object_type="DEAL",
        related_external_id="006xx0000000001AAA",
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={},
    )


@pytest.mark.asyncio
async def test_persist_single_note_writes_only_the_changed_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    note = _normalized_note()
    persist_calls: list[dict[str, object]] = []

    def fake_normalize_notes(raw_notes):
        assert raw_notes == [{"Id": note.external_id}]
        return [note], 0

    async def fake_persist(self, **kwargs):
        persist_calls.append(kwargs)
        return NoteWriteResult(notes_reactivated=1)

    monkeypatch.setattr(note_sync_service, "normalize_notes", fake_normalize_notes)
    monkeypatch.setattr(SalesforceNoteRecordSyncService, "_persist", fake_persist)

    result = await SalesforceNoteRecordSyncService().persist_single_note(
        organisation_id=org_id,
        integration_id=integration_id,
        raw_notes=[{"Id": note.external_id}],
    )

    assert result.notes_reactivated == 1
    assert persist_calls[0]["all_external_ids"] == [note.external_id]
    assert persist_calls[0]["normalized"] == [note]


@pytest.mark.asyncio
async def test_persist_loads_activity_maps_for_current_notes_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceNoteRecordSyncService()
    captured: dict[str, set[str]] = {}

    class FakeSession:
        async def commit(self) -> None:
            pass

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    class FakeWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, **_kwargs):
            return NoteWriteResult(notes_created=1)

    async def fake_load_user_map(session, organisation_id, integration_id, external_ids):
        captured["user_external_ids"] = set(external_ids)
        return {}

    async def fake_load_deal_map(session, organisation_id, integration_id, external_ids):
        captured["deal_external_ids"] = set(external_ids)
        return {}

    async def fake_load_contact_map(session, organisation_id, integration_id, external_ids):
        captured["contact_external_ids"] = set(external_ids)
        return {}

    async def fake_load_company_map(session, organisation_id, integration_id, external_ids):
        captured["company_external_ids"] = set(external_ids)
        return {}

    monkeypatch.setattr(note_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(note_sync_service, "SalesforceNoteWriter", FakeWriter)
    monkeypatch.setattr(service, "_load_user_map", fake_load_user_map)
    monkeypatch.setattr(service, "_load_deal_map", fake_load_deal_map)
    monkeypatch.setattr(service, "_load_contact_map", fake_load_contact_map)
    monkeypatch.setattr(service, "_load_company_map", fake_load_company_map)

    result = await service._persist(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        normalized=[
            _normalized_note(
                owner_external_id="005A",
                deal_external_id="006A",
                contact_external_id="003A",
                company_external_id="001A",
            )
        ],
        all_external_ids=["069A"],
    )

    assert result.notes_created == 1
    assert captured == {
        "user_external_ids": {"005A"},
        "deal_external_ids": {"006A"},
        "contact_external_ids": {"003A"},
        "company_external_ids": {"001A"},
    }
