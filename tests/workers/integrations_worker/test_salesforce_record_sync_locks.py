from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from src.service.integrations.crm.records.salesforce.tasks.task_types import TaskSyncResult
from src.workers.integrations_worker.config import TASK_LIMITS
from src.workers.integrations_worker.tasks.crm.salesforce import (
    salesforce_note_record_sync_task,
    salesforce_task_record_sync_task,
)


def _run_async(coro):
    return asyncio.run(coro)


def test_salesforce_task_sync_skips_when_same_integration_lock_is_held(monkeypatch):
    org_id = uuid4()
    integration_id = uuid4()
    captured: dict[str, object] = {}

    def fake_acquire(lock_key: str, ttl: int) -> bool:
        captured["lock_key"] = lock_key
        captured["ttl"] = ttl
        return False

    monkeypatch.setattr(salesforce_task_record_sync_task, "run_async", _run_async)
    monkeypatch.setattr(salesforce_task_record_sync_task, "acquire_task_lock", fake_acquire)
    monkeypatch.setattr(
        salesforce_task_record_sync_task.SalesforceTaskRecordSyncService,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    result = salesforce_task_record_sync_task.sync_salesforce_tasks_task.run(
        organisation_id=str(org_id),
        integration_id=str(integration_id),
    )

    assert result == {"status": "skipped", "reason": "already_running"}
    assert captured == {
        "lock_key": f"salesforce_record_sync:task:{org_id}:{integration_id}",
        "ttl": TASK_LIMITS["integrations.sync_salesforce_tasks"]["hard"] + 300,
    }


def test_salesforce_task_sync_releases_lock_after_success(monkeypatch):
    org_id = uuid4()
    integration_id = uuid4()
    released: list[str] = []

    async def fake_start_fresh(_session, _integration_id):
        return uuid4()

    async def fake_complete_fresh(*_args, **_kwargs):
        return None

    async def fake_update_metadata(**_kwargs):
        return None

    async def fake_acquire_session(_organisation_id):
        class Session:
            pass

        yield Session()

    async def fake_run(self, *, organisation_id, integration_id):
        return TaskSyncResult(
            provider="salesforce",
            object_type="task",
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(salesforce_task_record_sync_task, "run_async", _run_async)
    monkeypatch.setattr(
        salesforce_task_record_sync_task,
        "acquire_task_lock",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(salesforce_task_record_sync_task, "release_task_lock", released.append)
    monkeypatch.setattr(
        salesforce_task_record_sync_task,
        "acquire_org_async_session",
        fake_acquire_session,
    )
    monkeypatch.setattr(
        salesforce_task_record_sync_task.SyncHistoryManager,
        "start_fresh",
        fake_start_fresh,
    )
    monkeypatch.setattr(
        salesforce_task_record_sync_task.SyncHistoryManager,
        "complete_fresh",
        fake_complete_fresh,
    )
    monkeypatch.setattr(
        salesforce_task_record_sync_task, "_update_integration_metadata", fake_update_metadata
    )
    monkeypatch.setattr(
        salesforce_task_record_sync_task.SalesforceTaskRecordSyncService, "run", fake_run
    )

    result = salesforce_task_record_sync_task.sync_salesforce_tasks_task.run(
        organisation_id=str(org_id),
        integration_id=str(integration_id),
    )

    assert result["status"] == "success"
    assert released == [f"salesforce_record_sync:task:{org_id}:{integration_id}"]


def test_salesforce_note_sync_releases_lock_after_failure(monkeypatch):
    org_id = uuid4()
    integration_id = uuid4()
    released: list[str] = []

    async def fake_start_fresh(_session, _integration_id):
        return uuid4()

    async def fake_acquire_session(_organisation_id):
        class Session:
            pass

        yield Session()

    async def fake_failure_handler(**_kwargs):
        return None

    async def fake_run(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(salesforce_note_record_sync_task, "run_async", _run_async)
    monkeypatch.setattr(
        salesforce_note_record_sync_task,
        "acquire_task_lock",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(salesforce_note_record_sync_task, "release_task_lock", released.append)
    monkeypatch.setattr(
        salesforce_note_record_sync_task,
        "acquire_org_async_session",
        fake_acquire_session,
    )
    monkeypatch.setattr(
        salesforce_note_record_sync_task.SyncHistoryManager,
        "start_fresh",
        fake_start_fresh,
    )
    monkeypatch.setattr(
        salesforce_note_record_sync_task, "record_crm_task_failure", fake_failure_handler
    )
    monkeypatch.setattr(
        salesforce_note_record_sync_task.SalesforceNoteRecordSyncService, "run", fake_run
    )

    try:
        salesforce_note_record_sync_task.sync_salesforce_notes_task.run(
            organisation_id=str(org_id),
            integration_id=str(integration_id),
        )
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")

    assert released == [f"salesforce_record_sync:note:{org_id}:{integration_id}"]
