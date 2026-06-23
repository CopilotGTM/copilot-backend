from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMNote
from src.domain.models.organisation_tables.sync.crm.source_mappings import CRMSourceObjectType
from src.service.integrations.crm.records.salesforce.notes import note_writer
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    ActivityDimensionHit,
    NormalizedSalesforceNote,
)
from src.service.integrations.crm.records.salesforce.notes.note_writer import (
    _ASYNC_PG_BIND_PARAMETER_LIMIT,
    SalesforceNoteWriter,
)


def _scalar_result(rows: list[object]) -> SimpleNamespace:
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


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
        data_payload={"rows": [{"ContentDocumentId": external_id}]},
    )


def _normalized_note_with_link(
    *, external_id: str, link_id: str, deal_external_id: str | None
) -> NormalizedSalesforceNote:
    note = _normalized_note(external_id=external_id, deal_external_id=deal_external_id)
    note.data_payload["rows"] = [
        {
            "Id": link_id,
            "ContentDocumentId": external_id,
            "ContentDocumentLinkId": link_id,
        }
    ]
    return note


@pytest.mark.asyncio
async def test_writer_reactivates_existing_rows_and_soft_deletes_missing_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()

    reactivated = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Old note",
        body="Old body",
        is_deleted=True,
    )
    stale = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Old stale note",
        body="Old stale body",
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(
        side_effect=[MagicMock(rowcount=1), _scalar_result([stale.note_id]), MagicMock(rowcount=1)]
    )
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._projection.upsert_note = AsyncMock()  # type: ignore[method-assign]
    writer._projection.soft_delete_activity_projection = AsyncMock()  # type: ignore[method-assign]
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"069NOTE": reactivated, "069OLD": stale}
    )

    result = await writer.apply(
        normalized=[_normalized_note(external_id="069NOTE", deal_external_id="006OPPTY")],
        snapshot_started_at=datetime.now(UTC),
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
    assert reactivated.author_user_id == owner_id
    assert reactivated.author_name == "Alex Morgan"
    assert reactivated.author_email == "alex@example.com"
    assert reactivated.deal_id == deal_id
    assert reactivated.note_metadata["contact_id"] == str(contact_id)
    assert reactivated.note_metadata["company_id"] == str(company_id)
    assert reactivated.note_metadata["related_local_id"] == str(deal_id)
    assert reactivated.note_metadata["related_name"] == "Big Deal"
    assert stale.is_deleted is False
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert [row.external_id for row in mapped_rows] == ["069NOTE"]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.NOTE
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.NOTE
    assert mapped_rows[0].local_object_id == reactivated.note_id


@pytest.mark.asyncio
async def test_writer_upserts_note_link_source_mapping_for_content_document_link() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    note_id = uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]
    writer._note_ids_by_external = {"069NOTE": note_id}

    await writer.apply(
        normalized=[
            _normalized_note_with_link(
                external_id="069NOTE",
                link_id="06ALINK",
                deal_external_id="006OPPTY",
            )
        ],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert [row.external_object_type for row in mapped_rows] == [
        CRMSourceObjectType.NOTE,
        CRMSourceObjectType.NOTE_LINK,
    ]
    assert mapped_rows[1].external_id == "06ALINK"
    assert mapped_rows[1].local_object_type == CRMSourceObjectType.NOTE


@pytest.mark.asyncio
async def test_writer_single_record_mode_does_not_soft_delete_unrelated_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    active = CRMNote(
        note_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Active note",
        body="Active body",
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
        snapshot_started_at=datetime.now(UTC),
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
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[_normalized_note(external_id="069NOTE", deal_external_id="006OPPTY")],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    assert result.notes_created == 1
    writer._load_existing_rows.assert_awaited_once_with(external_ids={"069NOTE"})


@pytest.mark.asyncio
async def test_writer_uses_partial_index_bulk_upsert_for_current_batch() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[_normalized_note(external_id="069NOTE", deal_external_id="006OPPTY")],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    sql = str(session.execute.await_args_list[0].args[0].compile(dialect=writer._pg_dialect()))
    assert "ON CONFLICT" in sql
    assert "ON CONFLICT (note_id)" in sql
    assert "ON CONSTRAINT" not in sql
    assert result.notes_created == 1


@pytest.mark.asyncio
async def test_writer_splits_bulk_upsert_before_asyncpg_bind_parameter_limit() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    now = datetime.now(UTC)
    rows = [
        writer._row_values(
            note=_normalized_note(external_id=f"069NOTE{i}", deal_external_id="006OPPTY"),
            owner_hit=None,
            deal_hit=None,
            contact_hit=None,
            company_hit=None,
            related_local_id=None,
            related_name=None,
            last_seen_at=now,
        )
        for i in range(2000)
    ]

    await writer._bulk_upsert(rows)

    assert session.execute.await_count == 2
    for call in session.execute.await_args_list:
        compiled = call.args[0].compile(dialect=writer._pg_dialect())
        assert len(compiled.params) <= _ASYNC_PG_BIND_PARAMETER_LIMIT


@pytest.mark.asyncio
async def test_writer_upsert_does_not_clear_columns_outside_salesforce_note_mapping() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    await writer.apply(
        normalized=[_normalized_note(external_id="069NOTE", deal_external_id="006OPPTY")],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    sql = str(session.execute.await_args_list[0].args[0].compile(dialect=writer._pg_dialect()))
    assert "created_by_user_id = excluded.created_by_user_id" not in sql
    assert "body_format = excluded.body_format" not in sql
    assert "visibility = excluded.visibility" in sql


@pytest.mark.asyncio
async def test_writer_deactivates_rows_older_than_snapshot() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    snapshot_started_at = datetime.now(UTC)

    session = AsyncMock()
    note_ids = [uuid4(), uuid4()]
    session.execute = AsyncMock(side_effect=[_scalar_result(note_ids), MagicMock(rowcount=2)])
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._projection.soft_delete_activity_projection = AsyncMock()  # type: ignore[method-assign]
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[],
        snapshot_started_at=snapshot_started_at,
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=True,
    )

    assert result.notes_deactivated == 2
    statement = session.execute.await_args_list[0].args[0]
    sql = str(statement.compile(dialect=writer._pg_dialect()))
    assert "note_metadata" in sql
    assert "<" in sql
    assert "IS NULL" in sql
    assert "is_deleted = false" in sql


@pytest.mark.asyncio
async def test_writer_deactivates_missing_rows_only_for_scoped_deals() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    scoped_deal_id = uuid4()
    snapshot_started_at = datetime.now(UTC)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[],
        snapshot_started_at=snapshot_started_at,
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=True,
        deactivate_deal_ids={scoped_deal_id},
    )

    assert result.notes_deactivated == 1
    statement = session.execute.await_args_list[0].args[0]
    sql = str(statement.compile(dialect=writer._pg_dialect()))
    assert "crm_notes.deal_id IN" in sql


@pytest.mark.asyncio
async def test_writer_skips_deactivation_for_empty_scoped_deals() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    snapshot_started_at = datetime.now(UTC)

    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[],
        snapshot_started_at=snapshot_started_at,
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=True,
        deactivate_deal_ids=set(),
    )

    assert result.notes_deactivated == 0
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_load_existing_rows_chunks_local_id_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    rows = [
        CRMNote(
            note_id=uuid4(),
            organisation_id=organisation_id,
            integration_id=integration_id,
            title=f"Note {index}",
            body=f"Body {index}",
            is_deleted=False,
        )
        for index in range(3)
    ]

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows[:2])),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows[2:])),
        ]
    )

    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer.source_mappings = MagicMock()
    writer.source_mappings.load_local_id_map = AsyncMock(
        return_value={f"069NOTE{index}": row.note_id for index, row in enumerate(rows)}
    )
    monkeypatch.setattr(note_writer, "LOCAL_ID_QUERY_CHUNK_SIZE", 2)

    existing = await writer._load_existing_rows(external_ids=None)

    assert existing == {f"069NOTE{index}": row for index, row in enumerate(rows)}
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_deactivate_missing_chunks_scoped_deal_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    note_ids = [uuid4(), uuid4(), uuid4()]
    session.execute = AsyncMock(
        side_effect=[
            _scalar_result(note_ids[:2]),
            MagicMock(rowcount=2),
            _scalar_result(note_ids[2:]),
            MagicMock(rowcount=1),
        ]
    )
    writer = SalesforceNoteWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._projection.soft_delete_activity_projection = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(note_writer, "LOCAL_ID_QUERY_CHUNK_SIZE", 2)

    deactivated = await writer._deactivate_missing(
        datetime.now(UTC),
        deal_ids={uuid4(), uuid4(), uuid4()},
    )

    assert deactivated == 3
    assert session.execute.await_count == 4
