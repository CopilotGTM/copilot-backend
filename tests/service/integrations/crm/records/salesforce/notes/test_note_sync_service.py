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


def _normalized_note(external_id: str = "069xx0000000001AAA") -> NormalizedSalesforceNote:
    return NormalizedSalesforceNote(
        external_id=external_id,
        subject="QBR summary",
        body="Talked about renewal.",
        activity_subtype="SALESFORCE_NOTE",
        owner_external_id=None,
        deal_external_id="006xx0000000001AAA",
        contact_external_id=None,
        company_external_id=None,
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
