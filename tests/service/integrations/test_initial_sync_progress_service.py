from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    IntegrationType,
    KnowledgeIntegration,
    SyncStatus,
)
from src.service.integrations import initial_sync_progress_service as progress_module
from src.service.integrations.initial_sync_progress_service import (
    INITIAL_SYNC_KIND,
    InitialSyncProgressService,
)

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
INTEGRATION_ID = UUID("22222222-2222-2222-2222-222222222222")
SYNC_ID = UUID("33333333-3333-3333-3333-333333333333")


class FakeSession:
    def __init__(self, record: IntegrationSyncHistory | None = None) -> None:
        self.record = record
        self.added: list[IntegrationSyncHistory] = []
        self.committed = False
        self.statement = None

    def add(self, record: IntegrationSyncHistory) -> None:
        self.added.append(record)
        self.record = record

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _record: IntegrationSyncHistory) -> None:
        return None

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(scalar_one_or_none=lambda: self.record)


def make_integration() -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=INTEGRATION_ID,
        organisation_id=ORG_ID,
        created_by_user_id=UUID("44444444-4444-4444-4444-444444444444"),
        integration_type=IntegrationType.GOOGLE,
        name="Google",
        credentials={},
        connection_id=str(SYNC_ID),
    )


def make_record() -> IntegrationSyncHistory:
    return IntegrationSyncHistory(
        sync_id=SYNC_ID,
        organisation_id=ORG_ID,
        integration_id=INTEGRATION_ID,
        started_at=datetime(2026, 8, 4, 10, 0, 0),
        status=SyncStatus.QUEUED,
        sync_metadata=InitialSyncProgressService.new_metadata(["email", "calendar"]),
    )


@pytest.mark.asyncio
async def test_start_uses_oauth_attempt_as_sync_id_and_records_requested_branches(
    monkeypatch,
) -> None:
    session = FakeSession()
    published: list[dict] = []
    monkeypatch.setattr(progress_module, "_publish_snapshot", published.append)

    sync_id = await InitialSyncProgressService.start_run(
        session,
        integration=make_integration(),
        requested_branches=["email", "calendar"],
    )

    record = session.added[0]
    assert sync_id == SYNC_ID
    assert record.organisation_id == ORG_ID
    assert record.integration_id == INTEGRATION_ID
    assert record.status == SyncStatus.QUEUED
    assert record.sync_metadata["kind"] == INITIAL_SYNC_KIND
    assert record.sync_metadata["requested_capabilities"] == ["email", "calendar"]
    assert session.committed
    assert published[0]["sync_id"] == str(SYNC_ID)


@pytest.mark.asyncio
async def test_branch_updates_lock_parent_row_by_org_and_sync_id(monkeypatch) -> None:
    session = FakeSession(make_record())

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)

    await InitialSyncProgressService.mark_branch_started(ORG_ID, SYNC_ID, "email", 1)

    assert session.statement._for_update_arg is not None
    assert session.record.sync_metadata["branches"]["email"]["status"] == "syncing"
    assert session.record.status == SyncStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_register_chunks_prevents_provider_completion_from_ending_run(monkeypatch) -> None:
    record = make_record()
    session = FakeSession(record)

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)

    await InitialSyncProgressService.register_hydration(
        ORG_ID,
        SYNC_ID,
        "email",
        records_synced=12,
        chunk_keys=["email:0", "email:1"],
    )

    assert record.status == SyncStatus.IN_PROGRESS
    assert record.sync_metadata["branches"]["email"]["status"] == "hydrating"
    assert record.sync_metadata["metrics"]["emails_synced"] == 12


@pytest.mark.asyncio
async def test_completed_chunk_metrics_are_applied_only_once_on_redelivery(monkeypatch) -> None:
    record = make_record()
    session = FakeSession(record)

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)
    await InitialSyncProgressService.register_hydration(
        ORG_ID, SYNC_ID, "email", records_synced=3, chunk_keys=["email:0"]
    )

    for _ in range(2):
        await InitialSyncProgressService.complete_hydration_chunk(
            ORG_ID,
            SYNC_ID,
            "email",
            "email:0",
            contacts_created=2,
            accounts_created=1,
            emails_linked=3,
        )

    assert record.sync_metadata["metrics"]["contacts_created"] == 2
    assert record.sync_metadata["metrics"]["accounts_created"] == 1
    assert record.sync_metadata["metrics"]["emails_linked"] == 3


@pytest.mark.asyncio
async def test_provider_batch_count_joins_caller_transaction_and_is_retry_idempotent() -> None:
    """A committed provider batch must retain its first count across task redelivery."""
    record = make_record()
    session = FakeSession(record)

    for count in (12, 0):
        await InitialSyncProgressService.record_provider_batch_in_session(
            session,
            ORG_ID,
            SYNC_ID,
            "email",
            batch_key="folder:inbox",
            records_synced=count,
        )

    assert record.sync_metadata["metrics"]["emails_synced"] == 12
    assert record.sync_metadata["branches"]["email"]["records_synced"] == 12
    assert session.committed is False


@pytest.mark.asyncio
async def test_hydration_registration_cannot_overwrite_atomic_provider_count(monkeypatch) -> None:
    """A zero-delta retry may register work but cannot erase its first provider count."""
    record = make_record()
    session = FakeSession(record)
    await InitialSyncProgressService.record_provider_batch_in_session(
        session,
        ORG_ID,
        SYNC_ID,
        "email",
        batch_key="folder:inbox",
        records_synced=12,
    )

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)
    await InitialSyncProgressService.register_hydration(
        ORG_ID,
        SYNC_ID,
        "email",
        records_synced=0,
        chunk_keys=[],
    )

    assert record.sync_metadata["metrics"]["emails_synced"] == 12
    assert record.sync_metadata["branches"]["email"]["records_synced"] == 12


@pytest.mark.asyncio
async def test_chunk_metrics_can_commit_with_the_caller_transaction() -> None:
    """Creation flags and progress must share one commit so retries cannot erase counts."""
    record = make_record()
    record.sync_metadata["hydration_chunks"] = {"email:0": "queued"}
    record.sync_metadata["branches"]["email"].update(
        status="hydrating",
        hydration_expected=1,
    )
    session = FakeSession(record)

    await InitialSyncProgressService.complete_hydration_chunk_in_session(
        session,
        ORG_ID,
        SYNC_ID,
        "email",
        "email:0",
        contacts_created=4,
        accounts_created=2,
        emails_linked=7,
    )

    assert record.sync_metadata["metrics"]["contacts_created"] == 4
    assert record.sync_metadata["metrics"]["accounts_created"] == 2
    assert record.sync_metadata["metrics"]["emails_linked"] == 7
    assert session.committed is False


@pytest.mark.asyncio
async def test_terminal_rules_and_duration(monkeypatch) -> None:
    record = make_record()
    session = FakeSession(record)

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)
    monkeypatch.setattr(
        progress_module,
        "datetime_now_naive",
        lambda: datetime(2026, 8, 4, 10, 0, 5),
    )

    await InitialSyncProgressService.register_hydration(
        ORG_ID, SYNC_ID, "email", records_synced=4, chunk_keys=[]
    )
    await InitialSyncProgressService.fail_branch(
        ORG_ID, SYNC_ID, "calendar", error="permission denied"
    )

    assert record.status == SyncStatus.PARTIAL
    assert record.completed_at == datetime(2026, 8, 4, 10, 0, 5)
    assert InitialSyncProgressService.snapshot(record)["duration_ms"] == 5000


@pytest.mark.asyncio
async def test_publish_failure_does_not_rollback_committed_state(monkeypatch) -> None:
    session = FakeSession(make_record())

    async def sessions(_organisation_id):
        yield session

    def fail_publish(_snapshot):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", fail_publish)

    await InitialSyncProgressService.mark_branch_started(ORG_ID, SYNC_ID, "email", 1)

    assert session.committed
    assert session.record.status == SyncStatus.IN_PROGRESS


def test_redis_write_returning_none_is_a_publish_failure(monkeypatch) -> None:
    monkeypatch.setattr(progress_module, "xadd_json_frame", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Redis stream publish failed"):
        progress_module._publish_snapshot({"organisation_id": str(ORG_ID), "sync_id": str(SYNC_ID)})


@pytest.mark.asyncio
async def test_stale_recovery_terminalizes_open_branches_and_chunks(monkeypatch) -> None:
    record = make_record()
    record.sync_metadata["hydration_chunks"] = {"email:0": "queued"}
    session = FakeSession(record)

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)

    await InitialSyncProgressService.terminalize_stale_run(ORG_ID, SYNC_ID)

    assert record.status == SyncStatus.FAILED
    assert record.sync_metadata["hydration_chunks"]["email:0"] == "failed"
    assert all(branch["status"] == "failed" for branch in record.sync_metadata["branches"].values())


@pytest.mark.asyncio
async def test_terminal_run_ignores_late_chunk_success(monkeypatch) -> None:
    """Once the client receives complete, a late worker cannot rewrite status or duration."""
    record = IntegrationSyncHistory(
        sync_id=SYNC_ID,
        organisation_id=ORG_ID,
        integration_id=INTEGRATION_ID,
        started_at=datetime(2026, 8, 4, 10, 0, 0),
        status=SyncStatus.QUEUED,
        sync_metadata=InitialSyncProgressService.new_metadata(["email"]),
    )
    session = FakeSession(record)

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)
    monkeypatch.setattr(
        progress_module,
        "datetime_now_naive",
        lambda: datetime(2026, 8, 4, 10, 0, 5),
    )
    await InitialSyncProgressService.register_hydration(
        ORG_ID, SYNC_ID, "email", records_synced=1, chunk_keys=["email:0"]
    )
    await InitialSyncProgressService.fail_hydration_chunk(
        ORG_ID, SYNC_ID, "email", "email:0", error="worker failed"
    )
    completed_at = record.completed_at

    await InitialSyncProgressService.complete_hydration_chunk(
        ORG_ID,
        SYNC_ID,
        "email",
        "email:0",
        contacts_created=2,
        accounts_created=1,
        emails_linked=1,
    )

    assert record.status == SyncStatus.FAILED
    assert record.completed_at == completed_at
    assert record.sync_metadata["metrics"]["contacts_created"] == 0


@pytest.mark.asyncio
async def test_provider_redelivery_does_not_reopen_completed_hydration(monkeypatch) -> None:
    """Re-registering stable chunk keys must preserve their completed branch state."""
    record = make_record()
    session = FakeSession(record)

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", lambda _snapshot: None)
    await InitialSyncProgressService.register_hydration(
        ORG_ID, SYNC_ID, "email", records_synced=1, chunk_keys=["email:0"]
    )
    await InitialSyncProgressService.complete_hydration_chunk(
        ORG_ID,
        SYNC_ID,
        "email",
        "email:0",
        contacts_created=1,
        accounts_created=0,
        emails_linked=1,
    )

    await InitialSyncProgressService.register_hydration(
        ORG_ID, SYNC_ID, "email", records_synced=0, chunk_keys=["email:0"]
    )

    assert record.sync_metadata["branches"]["email"]["status"] == "completed"
    assert record.sync_metadata["branches"]["email"]["hydration_completed"] == 1


@pytest.mark.asyncio
async def test_raw_worker_exception_is_not_persisted_or_published(monkeypatch) -> None:
    """Provider exception details may contain PII or tokens and must stay out of progress."""
    record = make_record()
    session = FakeSession(record)
    published: list[dict] = []

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(progress_module, "acquire_org_async_session", sessions)
    monkeypatch.setattr(progress_module, "_publish_snapshot", published.append)
    await InitialSyncProgressService.fail_branch(
        ORG_ID,
        SYNC_ID,
        "email",
        error="access_token=secret-for-user@example.com",
    )

    branch = record.sync_metadata["branches"]["email"]
    assert branch["error_code"] == "unknown"
    assert "secret" not in branch["error"]
    assert "example.com" not in str(published)
