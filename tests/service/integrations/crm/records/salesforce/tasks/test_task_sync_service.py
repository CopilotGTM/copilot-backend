from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.tasks import task_sync_service
from src.service.integrations.crm.records.salesforce.tasks.task_sync_service import (
    SalesforceTaskRecordSyncService,
    SalesforceTaskSyncContext,
)
from src.service.integrations.crm.records.salesforce.tasks.task_types import (
    NormalizedSalesforceTask,
    TaskWriteResult,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo


def _normalized_task(
    external_id: str = "00Txx0000000001AAA",
    *,
    owner_external_id: str | None = None,
    deal_external_id: str | None = "006xx0000000001AAA",
    contact_external_id: str | None = None,
    company_external_id: str | None = None,
) -> NormalizedSalesforceTask:
    return NormalizedSalesforceTask(
        external_id=external_id,
        subject="Follow up",
        body=None,
        status="Not Started",
        priority=None,
        activity_subtype=None,
        owner_external_id=owner_external_id,
        due_date=None,
        completed_date=None,
        deal_external_id=deal_external_id,
        contact_external_id=contact_external_id,
        company_external_id=company_external_id,
        related_object_type="DEAL",
        related_external_id="006xx0000000001AAA",
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={},
    )


@pytest.mark.asyncio
async def test_run_persists_tasks_page_by_page_before_final_deactivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    first_task = _normalized_task(external_id="00T1", deal_external_id="006A")
    second_task = _normalized_task(external_id="00T2", deal_external_id="006B")
    persist_calls: list[dict[str, object]] = []

    class FakeFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def iter_batches(self):
            yield [{"Id": "00T1"}]
            yield [{"Id": "00T2"}]

    async def fake_load_context(self, organisation_id, integration_id):
        return SalesforceTaskSyncContext(
            token_info=IntegrationTokenInfo(
                access_token="token",
                refresh_token="refresh",
                integration_type="salesforce",
                organisation_id=organisation_id,
                integration_id=integration_id,
            ),
            instance_url="https://acme.my.salesforce.com",
        )

    def fake_normalize_tasks(raw_tasks):
        mapping = {"00T1": first_task, "00T2": second_task}
        return [mapping[row["Id"]] for row in raw_tasks], 0

    async def fake_persist(self, **kwargs):
        persist_calls.append(kwargs)
        return TaskWriteResult(tasks_created=len(kwargs["normalized"]))

    monkeypatch.setattr(task_sync_service, "SalesforceTasksFetcher", FakeFetcher)
    monkeypatch.setattr(SalesforceTaskRecordSyncService, "_load_context", fake_load_context)
    monkeypatch.setattr(task_sync_service, "normalize_tasks", fake_normalize_tasks)
    monkeypatch.setattr(SalesforceTaskRecordSyncService, "_persist", fake_persist)

    result = await SalesforceTaskRecordSyncService().run(
        organisation_id=org_id,
        integration_id=integration_id,
    )

    assert result.fetched_tasks == 2
    assert result.tasks_created == 2
    assert len(persist_calls) == 3
    assert persist_calls[0]["normalized"] == [first_task]
    assert persist_calls[0]["all_external_ids"] == ["00T1"]
    assert persist_calls[0]["deactivate_missing"] is False
    assert persist_calls[1]["normalized"] == [second_task]
    assert persist_calls[1]["all_external_ids"] == ["00T2"]
    assert persist_calls[1]["deactivate_missing"] is False
    assert persist_calls[2]["normalized"] == []
    assert persist_calls[2]["all_external_ids"] == {"00T1", "00T2"}
    assert persist_calls[2]["deactivate_missing"] is True


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


@pytest.mark.asyncio
async def test_persist_loads_activity_maps_for_current_tasks_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceTaskRecordSyncService()
    captured: dict[str, set[str]] = {}

    class FakeSession:
        async def commit(self) -> None:
            pass

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    class FakeWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, **_kwargs):
            return TaskWriteResult(tasks_created=1)

    async def fake_load_user_map(session, organisation_id, integration_id, external_ids):
        captured["user_external_ids"] = set(external_ids)
        return {}

    async def fake_load_deal_map(session, organisation_id, integration_id, external_ids):
        captured["deal_external_ids"] = set(external_ids)
        return {}

    async def fake_load_contact_map(session, organisation_id, integration_id, external_ids):
        captured["contact_external_ids"] = set(external_ids)
        return {}

    async def fake_load_company_map(session, organisation_id, integration_id, external_ids):
        captured["company_external_ids"] = set(external_ids)
        return {}

    monkeypatch.setattr(task_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(task_sync_service, "SalesforceTaskWriter", FakeWriter)
    monkeypatch.setattr(service, "_load_user_map", fake_load_user_map)
    monkeypatch.setattr(service, "_load_deal_map", fake_load_deal_map)
    monkeypatch.setattr(service, "_load_contact_map", fake_load_contact_map)
    monkeypatch.setattr(service, "_load_company_map", fake_load_company_map)

    result = await service._persist(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        normalized=[
            _normalized_task(
                owner_external_id="005A",
                deal_external_id="006A",
                contact_external_id="003A",
                company_external_id="001A",
            )
        ],
        all_external_ids=["00TA"],
    )

    assert result.tasks_created == 1
    assert captured == {
        "user_external_ids": {"005A"},
        "deal_external_ids": {"006A"},
        "contact_external_ids": {"003A"},
        "company_external_ids": {"001A"},
    }
