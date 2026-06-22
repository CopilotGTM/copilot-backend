"""Present CRM note source rows as deal-note API models."""

from __future__ import annotations

from typing import Any, cast

from src.api.schemas.crm_note_models import (
    DealNoteItem,
    DealNotesPage,
    DealParticipant,
    NoteAuthor,
    NoteMetadata,
)
from src.service.crm.notes.contracts import NoteReadRecord, ParticipantInfo


def _compact_preview(value: str | None, *, limit: int = 30) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact[:limit].rstrip() if compact else None


def note_display_title(note: Any) -> str | None:
    title = getattr(note, "title", None) or getattr(note, "subject", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    body = getattr(note, "body", None)
    return _compact_preview(body if isinstance(body, str) else None)


def note_source(note: Any) -> str:
    if getattr(note, "integration_id", None) is None:
        return "user"

    metadata = getattr(note, "note_metadata", None) or {}
    provider_raw = metadata.get("provider") if isinstance(metadata, dict) else None
    if isinstance(provider_raw, str):
        provider = provider_raw.strip().lower()
        if provider:
            return provider

    return "hubspot"


def _note_author_id(note: Any) -> str | None:
    author_user_id = getattr(note, "author_user_id", None)
    if author_user_id:
        return str(author_user_id)

    metadata = getattr(note, "note_metadata", None) or {}
    if not isinstance(metadata, dict):
        return None

    public_user_id = metadata.get("created_by_public_user_id")
    return public_user_id if isinstance(public_user_id, str) and public_user_id else None


def _unwrap_note(record: NoteReadRecord | Any) -> Any:
    if isinstance(record, NoteReadRecord):
        return record.note
    return record


def present_note_item(record: NoteReadRecord | Any) -> DealNoteItem:
    note = cast(Any, _unwrap_note(record))
    deal_id = getattr(note, "deal_id", None)
    if deal_id is None:
        raise ValueError("Note must have deal_id when presented as DealNoteItem")

    source = note_source(note)
    is_user_created = getattr(note, "integration_id", None) is None

    return DealNoteItem(
        note_id=note.note_id,
        deal_id=deal_id,
        title=getattr(note, "title", None),
        body=note.body,
        display_title=note_display_title(note),
        created_at=getattr(note, "source_created_at", None) or note.created_at,
        updated_at=getattr(note, "source_modified_at", None) or getattr(note, "updated_at", None),
        source=source,
        is_user_created=is_user_created,
        author=NoteAuthor(
            id=_note_author_id(note),
            name=getattr(note, "author_name", None),
            email=getattr(note, "author_email", None),
        ),
        metadata=NoteMetadata(
            synced_at=getattr(note, "source_modified_at", None) if not is_user_created else None,
        ),
    )


def present_deal_participant(participant: ParticipantInfo | Any) -> DealParticipant:
    source = cast(Any, participant)
    return DealParticipant(
        email=source.email,
        name=getattr(source, "name", None),
    )


def present_notes_page(
    *,
    notes: list[NoteReadRecord | Any],
    cursor: str | None,
    total_count: int,
    participants: list[ParticipantInfo | Any],
) -> DealNotesPage:
    return DealNotesPage(
        items=[present_note_item(note) for note in notes],
        cursor=cursor,
        total_count=total_count,
        participants=[present_deal_participant(participant) for participant in participants],
    )
