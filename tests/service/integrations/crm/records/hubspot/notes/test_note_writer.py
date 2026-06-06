"""Regression tests for HubSpot note writer snapshot boundaries.

These lock the difference between full snapshots, which may soft-delete absent
rows, and single-record incremental refreshes, which must only upsert one note.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
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
        subject=None,
        body="Spoke with buyer offline",
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
async def test_single_record_apply_does_not_soft_delete_unrelated_rows() -> None:
    """Single-record incremental refresh must not tombstone unrelated notes."""
    organisation_id = uuid4()
    integration_id = uuid4()

    updated = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="note-1",
        activity_type=CRMActivityType.NOTE.value,
        is_deleted=False,
    )
    unrelated = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="note-2",
        activity_type=CRMActivityType.NOTE.value,
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

    reactivated = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="note-1",
        activity_type=CRMActivityType.NOTE.value,
        is_deleted=True,
    )
    stale = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="note-2",
        activity_type=CRMActivityType.NOTE.value,
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
    assert reactivated.owner_id == owner_id
    assert reactivated.deal_id == deal_id
    assert reactivated.related_local_id == deal_id
    assert reactivated.related_name == "Big Deal"
    assert stale.is_deleted is True
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["external_object_type"] == (
        CRMSourceObjectType.NOTE
    )
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_type"] == (
        CRMSourceObjectType.ACTIVITY
    )
    mapping_helper.mark_missing_deleted.assert_awaited_once_with(
        object_type=CRMSourceObjectType.NOTE,
        seen_external_ids={"note-1"},
    )
