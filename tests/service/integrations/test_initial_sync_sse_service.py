from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.service.integrations import initial_sync_sse_service as sse_module
from src.service.integrations.initial_sync_progress_service import (
    InitialSyncProgressService,
)
from src.service.integrations.initial_sync_sse_service import InitialSyncSSEService

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("22222222-2222-2222-2222-222222222222")
SYNC_ID = UUID("33333333-3333-3333-3333-333333333333")


def make_record(status: SyncStatus = SyncStatus.IN_PROGRESS) -> IntegrationSyncHistory:
    record = IntegrationSyncHistory(
        sync_id=SYNC_ID,
        organisation_id=ORG_ID,
        integration_id=UUID("44444444-4444-4444-4444-444444444444"),
        started_at=datetime(2026, 8, 4, 10, 0, 0),
        status=status,
        sync_metadata=InitialSyncProgressService.new_metadata(["email"]),
    )
    if status == SyncStatus.SUCCESS:
        record.completed_at = datetime(2026, 8, 4, 10, 0, 1)
        record.sync_metadata["branches"]["email"]["status"] = "completed"
    return record


class FakeSession:
    def __init__(self, record):
        self.record = record
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(scalar_one_or_none=lambda: self.record)


@pytest.mark.asyncio
async def test_connector_can_read_own_initial_sync_run(monkeypatch) -> None:
    session = FakeSession(make_record())

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(sse_module, "acquire_org_async_session", sessions)
    service = InitialSyncSSEService(ORG_ID, SYNC_ID, USER_ID)

    assert await service.ensure_user_access() is True
    assert USER_ID in session.statement.compile().params.values()


@pytest.mark.asyncio
async def test_missing_or_other_user_run_fails_closed(monkeypatch) -> None:
    async def sessions(_org_id):
        yield FakeSession(None)

    monkeypatch.setattr(sse_module, "acquire_org_async_session", sessions)

    assert await InitialSyncSSEService(ORG_ID, SYNC_ID, USER_ID).ensure_user_access() is None


@pytest.mark.asyncio
async def test_authorization_database_failure_is_not_misreported_as_missing(monkeypatch) -> None:
    """A transient database outage must remain a server error, not a terminal 404."""

    async def sessions(_org_id):
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(sse_module, "acquire_org_async_session", sessions)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await InitialSyncSSEService(ORG_ID, SYNC_ID, USER_ID).ensure_user_access()


@pytest.mark.asyncio
async def test_completed_run_replays_snapshot_and_complete_without_redis(monkeypatch) -> None:
    async def load(_self):
        return make_record(SyncStatus.SUCCESS)

    monkeypatch.setattr(InitialSyncSSEService, "_get_record", load)
    service = InitialSyncSSEService(ORG_ID, SYNC_ID, USER_ID)

    events = [event async for event in service.stream_events()]

    assert [event["event"] for event in events] == ["ready", "snapshot", "complete"]
    assert service._streams is None


@pytest.mark.asyncio
async def test_active_stream_yields_durable_snapshot_before_redis_updates(monkeypatch) -> None:
    async def load(_self):
        return make_record()

    class FakeStreams:
        async def xread(self, _key, last_id, **_kwargs):
            assert last_id == "7-0"
            return [("8-0", {"event": "snapshot", "data": {"revision": 2, "status": "success"}})]

        async def close(self):
            return None

    monkeypatch.setattr(InitialSyncSSEService, "_get_record", load)
    monkeypatch.setattr(sse_module, "AsyncRedisStreams", FakeStreams)
    service = InitialSyncSSEService(ORG_ID, SYNC_ID, USER_ID)

    events = [event async for event in service.stream_events(resume_from_id="7-0")]

    assert [event["event"] for event in events] == [
        "ready",
        "snapshot",
        "snapshot",
        "complete",
    ]
    assert events[2]["id"] == "8-0"
