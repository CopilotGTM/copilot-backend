"""Regression tests for HubSpot task writer snapshot boundaries.

These lock the difference between full snapshots, which may soft-delete absent
rows, and single-record incremental refreshes, which must only upsert one task.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMTask
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.hubspot.tasks.task_types import (
    ActivityDimensionHit,
    NormalizedHubSpotTask,
)
from src.service.integrations.crm.records.hubspot.tasks.task_writer import HubSpotTaskWriter


def _normalized_task(*, external_id: str, deal_external_id: str | None) -> NormalizedHubSpotTask:
    return NormalizedHubSpotTask(
        external_id=external_id,
        subject="Follow up",
        body="Call customer",
        status="NOT_STARTED",
        priority="HIGH",
        activity_subtype="HUBSPOT_TASK",
        task_type="TODO",
        owner_external_id="owner-1",
        due_date=None,
        completed_date=None,
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


def _mock_source_mappings(writer: HubSpotTaskWriter) -> MagicMock:
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    return mapping_helper


@pytest.mark.asyncio
async def test_stale_task_snapshot_skips_without_writing_read_only_aliases() -> None:
    """Older provider snapshots should not crash by assigning CRMTask alias properties."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMTask(
        task_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Existing task",
        source_modified_at=datetime(2026, 6, 20, 10, tzinfo=UTC),
        is_deleted=False,
    )
    incoming = _normalized_task(external_id="task-1", deal_external_id=None)
    object.__setattr__(
        incoming,
        "source_modified_at",
        datetime(2026, 6, 20, 9, tzinfo=UTC),
    )

    session = AsyncMock()
    session.flush = AsyncMock()
    writer = HubSpotTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"task-1": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)
    writer._projection.upsert_task = AsyncMock()

    result = await writer.apply_single(
        task=incoming,
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
    )

    assert result.tasks_skipped_stale == 1
    writer._projection.upsert_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_record_apply_does_not_soft_delete_unrelated_rows() -> None:
    """Single-record incremental refresh must not tombstone unrelated tasks."""
    organisation_id = uuid4()
    integration_id = uuid4()

    updated = CRMTask(
        task_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Existing task",
        is_deleted=False,
    )
    unrelated = CRMTask(
        task_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Unrelated task",
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"task-1": updated, "task-2": unrelated}
    )
    _mock_source_mappings(writer)
    writer._projection.upsert_task = AsyncMock()
    writer._projection.soft_delete_activity_projection = AsyncMock()

    result = await writer.apply_single(
        task=_normalized_task(external_id="task-1", deal_external_id=None),
        user_map={},
        deal_map={},
        contact_map={},
        company_map={},
    )

    assert result.tasks_updated == 1
    assert result.tasks_deactivated == 0
    assert unrelated.is_deleted is False


@pytest.mark.asyncio
async def test_writer_reactivates_existing_rows_and_soft_deletes_missing_rows() -> None:
    """Full snapshots may reactivate seen tasks and soft-delete missing ones."""
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    owner_id = uuid4()

    reactivated = CRMTask(
        task_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Existing task",
        is_deleted=True,
    )
    stale = CRMTask(
        task_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        title="Missing task",
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"task-1": reactivated, "task-2": stale}
    )
    mapping_helper = _mock_source_mappings(writer)
    writer._projection.upsert_task = AsyncMock()
    writer._projection.soft_delete_activity_projection = AsyncMock()

    result = await writer.apply(
        normalized=[_normalized_task(external_id="task-1", deal_external_id="deal-1")],
        all_external_ids=["task-1"],
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

    assert result.tasks_reactivated == 1
    assert result.tasks_deactivated == 1
    assert reactivated.is_deleted is False
    assert reactivated.owner_user_id == owner_id
    assert reactivated.assigned_to_user_id == owner_id
    assert reactivated.deal_id == deal_id
    assert reactivated.priority == "HIGH"
    assert reactivated.task_metadata["hs_task_type"] == "TODO"
    assert reactivated.task_metadata["related_local_id"] == str(deal_id)
    assert reactivated.task_metadata["related_name"] == "Big Deal"
    assert stale.is_deleted is True
    writer._projection.upsert_task.assert_awaited_once_with(reactivated)
    writer._projection.soft_delete_activity_projection.assert_awaited_once_with(stale.task_id)
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["external_object_type"] == (
        CRMSourceObjectType.TASK
    )
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_type"] == (
        CRMSourceObjectType.TASK
    )
    mapping_helper.mark_missing_deleted.assert_awaited_once_with(
        object_type=CRMSourceObjectType.TASK,
        seen_external_ids={"task-1"},
    )
