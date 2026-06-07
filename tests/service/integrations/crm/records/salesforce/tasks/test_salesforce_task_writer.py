from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
from src.domain.models.organisation_tables.sync.crm.source_mappings import CRMSourceObjectType
from src.service.integrations.crm.records.salesforce.tasks.task_types import (
    ActivityDimensionHit,
    NormalizedSalesforceTask,
)
from src.service.integrations.crm.records.salesforce.tasks.task_writer import (
    _ASYNC_PG_BIND_PARAMETER_LIMIT,
    SalesforceTaskWriter,
)


def _normalized_task(*, external_id: str, deal_external_id: str | None) -> NormalizedSalesforceTask:
    return NormalizedSalesforceTask(
        external_id=external_id,
        subject="Follow up",
        body="Call customer",
        status="NOT_STARTED",
        priority="HIGH",
        activity_subtype="SALESFORCE_TASK",
        owner_external_id="005OWNER",
        due_date=None,
        completed_date=None,
        deal_external_id=deal_external_id,
        contact_external_id="003CONTACT",
        company_external_id="001ACCOUNT",
        related_object_type="DEAL" if deal_external_id else "CONTACT",
        related_external_id=deal_external_id or "003CONTACT",
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={"Id": external_id},
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
        external_id="00T1",
        activity_type=CRMActivityType.TASK.value,
        is_deleted=True,
    )
    stale = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="00T2",
        activity_type=CRMActivityType.TASK.value,
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(rowcount=1), MagicMock(rowcount=1)])
    session.flush = AsyncMock()

    writer = SalesforceTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"00T1": reactivated, "00T2": stale}
    )

    result = await writer.apply(
        normalized=[_normalized_task(external_id="00T1", deal_external_id="006OPPTY")],
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

    assert result.tasks_reactivated == 1
    assert result.tasks_deactivated == 1
    assert reactivated.is_deleted is False
    assert reactivated.owner_id == owner_id
    assert reactivated.deal_id == deal_id
    assert reactivated.contact_id == contact_id
    assert reactivated.company_id == company_id
    assert reactivated.related_local_id == deal_id
    assert reactivated.related_name == "Big Deal"
    assert stale.is_deleted is False
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert [row.external_id for row in mapped_rows] == ["00T1"]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.TASK
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.ACTIVITY
    assert mapped_rows[0].local_object_id == reactivated.activity_id


@pytest.mark.asyncio
async def test_writer_single_record_mode_does_not_soft_delete_unrelated_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    active = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="00T2",
        activity_type=CRMActivityType.TASK.value,
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"00T2": active})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    assert result.tasks_deactivated == 0
    assert active.is_deleted is False


@pytest.mark.asyncio
async def test_writer_chunk_mode_loads_only_current_external_ids() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceTaskWriter(
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
        normalized=[_normalized_task(external_id="00T1", deal_external_id="006OPPTY")],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    assert result.tasks_created == 1
    writer._load_existing_rows.assert_awaited_once_with(external_ids={"00T1"})


@pytest.mark.asyncio
async def test_writer_uses_partial_index_bulk_upsert_for_current_batch() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.flush = AsyncMock()

    writer = SalesforceTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[_normalized_task(external_id="00T1", deal_external_id="006OPPTY")],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    sql = str(session.execute.await_args_list[0].args[0].compile(dialect=writer._pg_dialect()))
    assert "ON CONFLICT" in sql
    assert "ON CONFLICT (activity_id)" in sql
    assert "ON CONSTRAINT" not in sql
    assert result.tasks_created == 1


@pytest.mark.asyncio
async def test_writer_splits_bulk_upsert_before_asyncpg_bind_parameter_limit() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

    writer = SalesforceTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    now = datetime.now(UTC)
    rows = [
        writer._row_values(
            task=_normalized_task(external_id=f"00T{i}", deal_external_id="006OPPTY"),
            owner_hit=None,
            deal_hit=None,
            contact_hit=None,
            company_hit=None,
            related_local_id=None,
            related_name=None,
            now=now,
            last_seen_at=now,
        )
        for i in range(1000)
    ]

    await writer._bulk_upsert(rows)

    assert session.execute.await_count == 2
    for call in session.execute.await_args_list:
        compiled = call.args[0].compile(dialect=writer._pg_dialect())
        assert len(compiled.params) <= _ASYNC_PG_BIND_PARAMETER_LIMIT


@pytest.mark.asyncio
async def test_writer_upsert_does_not_clear_columns_outside_salesforce_task_mapping() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.flush = AsyncMock()

    writer = SalesforceTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

    await writer.apply(
        normalized=[_normalized_task(external_id="00T1", deal_external_id="006OPPTY")],
        snapshot_started_at=datetime.now(UTC),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
        deactivate_missing=False,
    )

    sql = str(session.execute.await_args_list[0].args[0].compile(dialect=writer._pg_dialect()))
    assert "created_by_user_id = excluded.created_by_user_id" not in sql
    assert "attendees = excluded.attendees" not in sql
    assert "is_all_day = excluded.is_all_day" not in sql
    assert "direction = excluded.direction" not in sql


@pytest.mark.asyncio
async def test_writer_deactivates_rows_older_than_snapshot() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    snapshot_started_at = datetime.now(UTC)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=3))
    session.flush = AsyncMock()

    writer = SalesforceTaskWriter(
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
    )

    assert result.tasks_deactivated == 3
    statement = session.execute.await_args_list[0].args[0]
    sql = str(statement.compile(dialect=writer._pg_dialect()))
    assert "last_seen_at <" in sql
    assert "last_seen_at IS NULL" in sql
    assert "is_deleted = false" in sql
