"""Regression coverage for bounded, tenant-scoped initial-sync recovery."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import SyncStatus
from src.workers.integrations_worker.tasks import initial_sync_recovery_tasks as recovery


@pytest.mark.asyncio
async def test_stale_query_is_tenant_scoped_and_open_status_only(monkeypatch) -> None:
    """Recovery must never select another tenant or rewrite a terminal run."""
    organisation_id = uuid4()
    sync_id = uuid4()
    captured = SimpleNamespace(statement=None)

    class Session:
        async def execute(self, statement):
            captured.statement = statement
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [sync_id]))

    async def sessions(_organisation_id):
        yield Session()

    monkeypatch.setattr(recovery, "acquire_org_async_session", sessions)

    assert await recovery._stale_sync_ids(organisation_id, 10) == [sync_id]
    params = list(captured.statement.compile().params.values())
    assert organisation_id in params
    status_values = next(value for value in params if isinstance(value, list))
    assert SyncStatus.QUEUED in status_values
    assert SyncStatus.IN_PROGRESS in status_values


def test_recovery_terminalizes_each_selected_run_once(monkeypatch) -> None:
    """The global task lock and per-org queries bound one recovery pass to its selection."""
    organisation_ids = [uuid4(), uuid4()]
    stale_by_org = {organisation_id: [uuid4()] for organisation_id in organisation_ids}
    terminalized: list[tuple] = []

    class MasterSession:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: organisation_ids))

    async def master_sessions():
        yield MasterSession()

    async def stale(organisation_id, _limit):
        return stale_by_org[organisation_id]

    async def terminalize(organisation_id, sync_id):
        terminalized.append((organisation_id, sync_id))

    monkeypatch.setattr(recovery, "acquire_master_async_session", master_sessions)
    monkeypatch.setattr(recovery, "_stale_sync_ids", stale)
    monkeypatch.setattr(
        recovery.InitialSyncProgressService,
        "terminalize_stale_run",
        terminalize,
    )
    monkeypatch.setattr(recovery, "acquire_task_lock", lambda *_args, **_kwargs: True)
    release = SimpleNamespace(calls=[])
    monkeypatch.setattr(
        recovery,
        "release_task_lock",
        lambda key: release.calls.append(key),
    )
    monkeypatch.setattr(recovery, "run_async", asyncio.run)

    result = recovery.recover_stale_initial_syncs_task.run()

    assert result == {"status": "success", "terminalized": 2}
    assert terminalized == [
        (organisation_id, stale_by_org[organisation_id][0]) for organisation_id in organisation_ids
    ]
    assert release.calls == ["task_lock:recover_stale_initial_syncs"]
