from __future__ import annotations

from datetime import UTC, datetime

from src.service.integrations.crm.records.salesforce.notes.note_normalizer import normalize_notes


def test_normalize_notes_groups_multiple_links_by_document_and_uses_deal_precedence() -> None:
    normalized, skipped = normalize_notes(
        [
            {
                "ContentDocumentId": "069NOTE",
                "LinkedEntityId": "006OPPTY",
                "Title": "QBR summary",
                "Content": "Talked about renewal and pricing.",
                "OwnerId": "005OWNER",
                "CreatedDate": "2026-04-20T10:00:00.000+0000",
                "LastModifiedDate": "2026-04-21T12:00:00.000+0000",
            },
            {
                "ContentDocumentId": "069NOTE",
                "LinkedEntityId": "003CONTACT",
                "Title": "QBR summary",
                "Content": "Talked about renewal and pricing.",
                "OwnerId": "005OWNER",
                "CreatedDate": "2026-04-20T10:00:00.000+0000",
                "LastModifiedDate": "2026-04-21T12:00:00.000+0000",
            },
            {
                "ContentDocumentId": "069NOTE",
                "LinkedEntityId": "001ACCOUNT",
                "Title": "QBR summary",
                "Content": "Talked about renewal and pricing.",
                "OwnerId": "005OWNER",
                "CreatedDate": "2026-04-20T10:00:00.000+0000",
                "LastModifiedDate": "2026-04-21T12:00:00.000+0000",
            },
        ]
    )

    assert skipped == 0
    assert len(normalized) == 1
    note = normalized[0]
    assert note.external_id == "069NOTE"
    assert note.subject == "QBR summary"
    assert note.body == "Talked about renewal and pricing."
    assert note.owner_external_id == "005OWNER"
    assert note.deal_external_id == "006OPPTY"
    assert note.contact_external_id == "003CONTACT"
    assert note.company_external_id == "001ACCOUNT"
    assert note.related_object_type == "DEAL"
    assert note.related_external_id == "006OPPTY"
    assert note.source_created_at == datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    assert note.source_modified_at == datetime(2026, 4, 21, 12, 0, tzinfo=UTC)


def test_normalize_notes_skips_rows_without_document_id() -> None:
    normalized, skipped = normalize_notes([{"Title": "Missing id"}])

    assert normalized == []
    assert skipped == 1
