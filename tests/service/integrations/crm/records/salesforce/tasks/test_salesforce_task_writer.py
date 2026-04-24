from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
from src.service.integrations.crm.records.salesforce.tasks.task_types import (
    ActivityDimensionHit,
    NormalizedSalesforceTask,
)
from src.service.integrations.crm.records.salesforce.tasks.task_writer import SalesforceTaskWriter


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
    session.flush = AsyncMock()

    writer = SalesforceTaskWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"00T1": reactivated, "00T2": stale}
    )

    result = await writer.apply(
        normalized=[_normalized_task(external_id="00T1", deal_external_id="006OPPTY")],
        all_external_ids=["00T1"],
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
    assert stale.is_deleted is True
