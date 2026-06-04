from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    ActivityDimensionHit,
    NormalizedSalesforceNote,
)
from src.service.integrations.crm.records.salesforce.notes.note_writer import (
    SalesforceNoteWriter,
)


def _normalized_note(*, external_id: str, deal_external_id: str | None) -> NormalizedSalesforceNote:
    return NormalizedSalesforceNote(
        external_id=external_id,
        subject="QBR summary",
        body="Talked about renewal and pricing.",
        activity_subtype="SALESFORCE_NOTE",
        owner_external_id="005OWNER",
        deal_external_id=deal_external_id,
        contact_external_id="003CONTACT",
        company_external_id="001ACCOUNT",
        related_object_type="DEAL" if deal_external_id else "CONTACT",
        related_external_id=deal_external_id or "003CONTACT",
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={"ContentDocumentId": external_id},
    )


@pytest.mark.asyncio
async def test_writer_reactivates_existing_rows_and_soft_deletes_missing_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()

    reactivated = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="069NOTE",
        activity_type=CRMActivityType.NOTE.value,
        is_deleted=True,
    )
    stale = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="069OLD",
        activity_type=CRMActivityType.NOTE.value,
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"069NOTE": reactivated, "069OLD": stale}
    )

    result = await writer.apply(
        normalized=[_normalized_note(external_id="069NOTE", deal_external_id="006OPPTY")],
        all_external_ids=["069NOTE"],
        user_map={
            "005OWNER": ActivityDimensionHit(
                internal_id=owner_id,
                name="Alex Morgan",
                email="alex@example.com",
            )
        },
        deal_map={"006OPPTY": ActivityDimensionHit(internal_id=deal_id, name="Big Deal")},
        contact_map={
            "003CONTACT": ActivityDimensionHit(
                internal_id=contact_id,
                name="Taylor Contact",
                email="taylor@example.com",
            )
        },
        company_map={"001ACCOUNT": ActivityDimensionHit(internal_id=company_id, name="Acme")},
    )

    assert result.notes_reactivated == 1
    assert result.notes_deactivated == 1
    assert reactivated.is_deleted is False
    assert reactivated.owner_id == owner_id
    assert reactivated.deal_id == deal_id
    assert reactivated.contact_id == contact_id
    assert reactivated.company_id == company_id
    assert reactivated.related_local_id == deal_id
    assert reactivated.related_name == "Big Deal"
    assert stale.is_deleted is True


@pytest.mark.asyncio
async def test_writer_single_record_mode_does_not_soft_delete_unrelated_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    active = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="069OLD",
        activity_type=CRMActivityType.NOTE.value,
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"069OLD": active})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[],
        all_external_ids=[],
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    assert result.notes_deactivated == 0
    assert active.is_deleted is False


@pytest.mark.asyncio
async def test_writer_chunk_mode_loads_only_current_external_ids() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[_normalized_note(external_id="069NOTE", deal_external_id="006OPPTY")],
        all_external_ids=["069NOTE"],
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    assert result.notes_created == 1
    writer._load_existing_rows.assert_awaited_once_with(external_ids={"069NOTE"})
