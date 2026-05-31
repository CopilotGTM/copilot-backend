from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.tasks import task_sync_service
from src.service.integrations.crm.records.salesforce.tasks.task_sync_service import (
    SalesforceTaskRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.tasks.task_types import (
    NormalizedSalesforceTask,
    TaskWriteResult,
)


def _normalized_task(external_id: str = "00Txx0000000001AAA") -> NormalizedSalesforceTask:
    return NormalizedSalesforceTask(
        external_id=external_id,
        subject="Follow up",
        body=None,
        status="Not Started",
        priority=None,
        activity_subtype=None,
        owner_external_id=None,
        due_date=None,
        completed_date=None,
        deal_external_id="006xx0000000001AAA",
        contact_external_id=None,
        company_external_id=None,
        related_object_type="DEAL",
        related_external_id="006xx0000000001AAA",
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={},
    )


@pytest.mark.asyncio
async def test_persist_single_task_writes_only_the_changed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    task = _normalized_task()
    persist_calls: list[dict[str, object]] = []

    def fake_normalize_tasks(raw_tasks):
        assert raw_tasks == [{"Id": task.external_id}]
        return [task], 0

    async def fake_persist(self, **kwargs):
        persist_calls.append(kwargs)
        return TaskWriteResult(tasks_updated=1)

    monkeypatch.setattr(task_sync_service, "normalize_tasks", fake_normalize_tasks)
    monkeypatch.setattr(SalesforceTaskRecordSyncService, "_persist", fake_persist)

    result = await SalesforceTaskRecordSyncService().persist_single_task(
        organisation_id=org_id,
        integration_id=integration_id,
        raw_task={"Id": task.external_id},
    )

    assert result.tasks_updated == 1
    assert persist_calls[0]["all_external_ids"] == [task.external_id]
    assert persist_calls[0]["normalized"] == [task]
