from __future__ import annotations

from datetime import UTC, datetime

from src.service.integrations.crm.records.hubspot.notes.note_normalizer import normalize_notes


def test_normalize_notes_maps_body_and_related_object_precedence() -> None:
    normalized, skipped = normalize_notes(
        [
            {
                "id": "123",
                "properties": {
                    "hs_note_body": "Spoke with buyer offline",
                    "hubspot_owner_id": "456",
                    "hs_createdate": "2026-04-20T10:00:00Z",
                    "hs_lastmodifieddate": "2026-04-21T12:00:00Z",
                },
                "associations": {
                    "deals": {"results": [{"id": "deal-1"}]},
                    "contacts": {"results": [{"id": "contact-1"}]},
                    "companies": {"results": [{"id": "company-1"}]},
                },
            }
        ]
    )

    assert skipped == 0
    assert len(normalized) == 1
    note = normalized[0]
    assert note.external_id == "123"
    assert note.subject is None
    assert note.body == "Spoke with buyer offline"
    assert note.owner_external_id == "456"
    assert note.deal_external_id == "deal-1"
    assert note.contact_external_id == "contact-1"
    assert note.company_external_id == "company-1"
    assert note.related_object_type == "DEAL"
    assert note.related_external_id == "deal-1"
    assert note.source_created_at == datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    assert note.source_modified_at == datetime(2026, 4, 21, 12, 0, tzinfo=UTC)


def test_normalize_notes_skips_rows_without_id() -> None:
    normalized, skipped = normalize_notes([{"properties": {"hs_note_body": "Missing id"}}])

    assert normalized == []
    assert skipped == 1
