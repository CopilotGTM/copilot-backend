from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
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
        priority=None,
        activity_subtype="HUBSPOT_TASK",
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


@pytest.mark.asyncio
async def test_writer_reactivates_existing_rows_and_soft_deletes_missing_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    owner_id = uuid4()

    reactivated = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="task-1",
        activity_type=CRMActivityType.TASK.value,
        is_deleted=True,
    )
    stale = CRMActivity(
        activity_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="task-2",
        activity_type=CRMActivityType.TASK.value,
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
    assert reactivated.owner_id == owner_id
    assert reactivated.deal_id == deal_id
    assert reactivated.related_local_id == deal_id
    assert reactivated.related_name == "Big Deal"
    assert stale.is_deleted is True
