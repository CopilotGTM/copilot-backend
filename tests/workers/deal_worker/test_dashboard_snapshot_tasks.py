from __future__ import annotations

import importlib
from uuid import uuid4

from src.workers.deal_worker.tasks.dashboard_snapshot_tasks import (
    DASHBOARD_SNAPSHOT_EXCLUDED_ORG_IDS,
)


def test_refresh_sales_snapshot_org_task_renders_system_sales_snapshot(monkeypatch) -> None:
    task_module = importlib.import_module("src.workers.deal_worker.tasks.dashboard_snapshot_tasks")
    organisation_id = uuid4()
    calls = []
    logs = []

    class FakeDashboardSnapshotService:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def regenerate_sales_snapshot(self, *, user_id=None):
            calls.append(("regenerate", user_id))
            return object()

    class FakeSession:
        pass

    async def fake_master_sessions():
        yield FakeSession()

    async def fake_org_sessions(org_id):
        assert org_id == organisation_id
        yield FakeSession()

    monkeypatch.setattr(task_module, "DashboardSnapshotService", FakeDashboardSnapshotService)
    monkeypatch.setattr(task_module, "acquire_master_async_session", fake_master_sessions)
    monkeypatch.setattr(task_module, "acquire_org_async_session", fake_org_sessions)
    monkeypatch.setattr(
        task_module.logger,
        "info",
        lambda message, *, extra: logs.append((message, extra)),
    )

    result = task_module.refresh_sales_snapshot_org_task.run(str(organisation_id))

    assert result == {"status": "success", "organisation_id": str(organisation_id)}
    assert calls[0][0] == "init"
    assert calls[0][1]["organisation_id"] == organisation_id
    assert calls[1] == ("regenerate", None)
    assert logs[0][1]["duration_seconds"] >= 0


def test_refresh_sales_snapshots_all_orgs_skips_dashboard_excluded_orgs(monkeypatch) -> None:
    task_module = importlib.import_module("src.workers.deal_worker.tasks.dashboard_snapshot_tasks")
    included_org_id = uuid4()
    excluded_org_id = next(iter(DASHBOARD_SNAPSHOT_EXCLUDED_ORG_IDS))
    dispatched = []

    class FakeResult:
        def all(self):
            return [(included_org_id,), (excluded_org_id,)]

    class FakeMasterSession:
        async def execute(self, stmt):
            return FakeResult()

    class FakeAsyncTask:
        id = "task-1"

    async def fake_master_sessions():
        yield FakeMasterSession()

    def fake_apply_async(*, kwargs, queue, headers):
        dispatched.append((kwargs, queue, headers))
        return FakeAsyncTask()

    monkeypatch.setattr(task_module, "acquire_master_async_session", fake_master_sessions)
    monkeypatch.setattr(
        task_module.refresh_sales_snapshot_org_task, "apply_async", fake_apply_async
    )
    monkeypatch.setattr(task_module, "correlation_headers", lambda: {"x-request-id": "test"})

    result = task_module.refresh_sales_snapshots_all_orgs_task.run()

    assert result["orgs_found"] == 1
    assert result["orgs_triggered"] == 1
    assert dispatched[0][0] == {"organisation_id": str(included_org_id)}
