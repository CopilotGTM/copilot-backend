"""Regression tests for HubSpot note writer snapshot boundaries.

These lock the difference between full snapshots, which may soft-delete absent
rows, and single-record incremental refreshes, which must only upsert one note.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMNote
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.hubspot.notes.note_types import (
    ActivityDimensionHit,
    NormalizedHubSpotNote,
)
from src.service.integrations.crm.records.hubspot.notes.note_writer import HubSpotNoteWriter


def _normalized_note(*, external_id: str, deal_external_id: str | None) -> NormalizedHubSpotNote:
    return NormalizedHubSpotNote(
        external_id=external_id,
        subject="Spoke with buyer offline",
        body="Spoke with buyer offline",
        title="Spoke with buyer offline",
        body_format="html",
        attachment_ids=["attachment-1"],
        activity_subtype="HUBSPOT_NOTE",
        owner_external_id="owner-1",
        deal_external_id=deal_external_id,
        contact_external_id=None,
        company_external_id=None,
        related_object_type="DEAL" if deal_external_id else None,
        related_external_id=deal_external_id,
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={"id": external_id},
    )


def _mock_source_mappings(writer: HubSpotNoteWriter) -> MagicMock:
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    return mapping_helper


@pytest.mark.asyncio
async def test_stale_note_snapshot_skips_without_writing_read_only_aliases() -> None:
    """Older provider snapshots should not crash by assigning CRMNote alias properties."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Existing note",
        body="Existing body",
        source_modified_at=datetime(2026, 6, 20, 10, tzinfo=UTC),
        is_deleted=False,
    )
    incoming = _normalized_note(external_id="note-1", deal_external_id=None)
    object.__setattr__(
        incoming,
        "source_modified_at",
        datetime(2026, 6, 20, 9, tzinfo=UTC),
    )

    session = AsyncMock()
    session.flush = AsyncMock()
    writer = HubSpotNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"note-1": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)
    writer._projection.upsert_note = AsyncMock()

    result = await writer.apply_single(
        note=incoming,
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
    )

    assert result.notes_skipped_stale == 1
    writer._projection.upsert_note.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_record_apply_does_not_soft_delete_unrelated_rows() -> None:
    """Single-record incremental refresh must not tombstone unrelated notes."""
    organisation_id = uuid4()
    integration_id = uuid4()

    updated = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Existing note",
        body="Existing body",
        is_deleted=False,
    )
    unrelated = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Unrelated note",
        body="Unrelated body",
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"note-1": updated, "note-2": unrelated}
    )
    _mock_source_mappings(writer)
    writer._projection.upsert_note = AsyncMock()
    writer._projection.soft_delete_activity_projection = AsyncMock()

    result = await writer.apply_single(
        note=_normalized_note(external_id="note-1", deal_external_id=None),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
    )

    assert result.notes_updated == 1
    assert result.notes_deactivated == 0
    assert unrelated.is_deleted is False


@pytest.mark.asyncio
async def test_writer_reactivates_existing_rows_and_soft_deletes_missing_rows() -> None:
    """Full snapshots may reactivate seen notes and soft-delete missing ones."""
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    owner_id = uuid4()

    reactivated = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Existing note",
        body="Existing body",
        is_deleted=True,
    )
    stale = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Missing note",
        body="Missing body",
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"note-1": reactivated, "note-2": stale}
    )
    mapping_helper = _mock_source_mappings(writer)
    writer._projection.upsert_note = AsyncMock()
    writer._projection.soft_delete_activity_projection = AsyncMock()

    result = await writer.apply(
        normalized=[_normalized_note(external_id="note-1", deal_external_id="deal-1")],
        all_external_ids=["note-1"],
        user_map={
            "owner-1": ActivityDimensionHit(
                internal_id=owner_id,
                name="Alex Morgan",
                email="alex@example.com",
            )
        },
        deal_map={"deal-1": ActivityDimensionHit(internal_id=deal_id, name="Big Deal")},
        contact_map={},
        company_map={},
    )

    assert result.notes_reactivated == 1
    assert result.notes_deactivated == 1
    assert reactivated.is_deleted is False
    assert reactivated.author_user_id == owner_id
    assert reactivated.deal_id == deal_id
    assert reactivated.title == "Spoke with buyer offline"
    assert reactivated.body_format == "html"
    assert reactivated.note_metadata["hs_attachment_ids"] == ["attachment-1"]
    assert reactivated.note_metadata["related_local_id"] == str(deal_id)
    assert reactivated.note_metadata["related_name"] == "Big Deal"
    assert stale.is_deleted is True
    writer._projection.upsert_note.assert_awaited_once_with(reactivated)
    writer._projection.soft_delete_activity_projection.assert_awaited_once_with(stale.note_id)
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["external_object_type"] == (
        CRMSourceObjectType.NOTE
    )
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_type"] == (
        CRMSourceObjectType.NOTE
    )
    mapping_helper.mark_missing_deleted.assert_awaited_once_with(
        object_type=CRMSourceObjectType.NOTE,
        seen_external_ids={"note-1"},
    )
