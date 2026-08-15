"""Regression tests for durable chat-attachment post-upload recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.workers.chat_worker.services.stale_job_cleanup_service import (
    ATTACHMENT_PROCESSING_REQUEUE_AFTER_MINUTES,
    ATTACHMENT_RECOVERY_BATCH_SIZE,
    ChatStaleJobCleanupService,
)


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        """Return the leased attachments through SQLAlchemy's scalar-result shape."""
        return SimpleNamespace(all=lambda: self.values)


class _LeaseSession:
    def __init__(self, attachments):
        self.attachments = attachments
        self.statement = None
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        """Capture the lease query and return the configured attachments."""
        self.statement = statement
        return _Result(self.attachments)

    def add(self, value):
        """Record attachment updates staged by the recovery lease."""
        self.added.append(value)

    async def commit(self):
        """Record that the lease was persisted before task publication."""
        self.commits += 1


class _EmptyRowsResult:
    def all(self):
        return []

    def scalars(self):
        return self


class _QuerySession:
    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _EmptyRowsResult()


@pytest.mark.asyncio
async def test_attachment_recovery_passes_dispatcher_timeout(monkeypatch) -> None:
    """Recovery must let the shared dispatcher own the publication deadline."""
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    organisation_id = uuid4()
    attachment_id = uuid4()
    publish = AsyncMock(return_value="task-id")
    monkeypatch.setattr("src.workers.chat_worker.client.send_chat_task_async", publish)

    task_id = await recovery_module.enqueue_attachment_post_upload(
        organisation_id,
        attachment_id,
    )

    assert task_id == "task-id"
    publish.assert_awaited_once_with(
        "attachments.post_upload",
        args=[str(organisation_id), str(attachment_id)],
        queue="attachments",
        routing_key="attachments",
        timeout_seconds=6.0,
    )


@pytest.mark.asyncio
async def test_stale_attachment_recovery_leases_a_bounded_processing_batch(monkeypatch) -> None:
    """Recovery must commit an org-scoped row lease before publishing to Redis."""
    organisation_id = uuid4()
    service = ChatStaleJobCleanupService(organisation_id)
    old_updated_at = datetime.now(UTC) - timedelta(minutes=20)
    attachment = SimpleNamespace(attachment_id=uuid4(), updated_at=old_updated_at)
    session = _LeaseSession([attachment])

    async def sessions(_organisation_id):
        """Yield the recording session through the production context-manager API."""
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )
    now = datetime.now(UTC)

    attachment_ids = await service._lease_stale_attachments(now)

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "ATTACHMENTS.STATUS = 'PROCESSING'" in sql
    assert "ATTACHMENTS.IS_DELETED IS FALSE" in sql
    assert "ATTACHMENTS.STORAGE_PATH != ''" in sql
    assert f"LIMIT {ATTACHMENT_RECOVERY_BATCH_SIZE}" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert attachment_ids == [attachment.attachment_id]
    assert attachment.updated_at == now
    assert session.added == [attachment]
    assert session.commits == 1
    assert ATTACHMENT_PROCESSING_REQUEUE_AFTER_MINUTES == 12


@pytest.mark.asyncio
async def test_stale_attachment_recovery_keeps_publish_failures_retryable(monkeypatch) -> None:
    """One broker failure must not prevent other leased attachments from publishing."""
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    service = ChatStaleJobCleanupService(uuid4())
    attachment_ids = [uuid4(), uuid4()]
    monkeypatch.setattr(
        service,
        "_lease_stale_attachments",
        AsyncMock(return_value=attachment_ids),
    )
    publish = AsyncMock(side_effect=[RuntimeError("broker unavailable"), "task-2"])
    monkeypatch.setattr(recovery_module, "enqueue_attachment_post_upload", publish)

    counts = await service._recover_stale_attachments(datetime.now(UTC))

    assert counts == {"requeued": 1, "requeue_failed": 1}
    assert [call.args for call in publish.await_args_list] == [
        (service.organisation_id, attachment_ids[0]),
        (service.organisation_id, attachment_ids[1]),
    ]


@pytest.mark.asyncio
async def test_cleanup_shares_one_budget_across_all_recovery_passes(monkeypatch) -> None:
    """One organisation cannot bypass the sweep budget through multiple recovery queries."""
    service = ChatStaleJobCleanupService(uuid4())
    attachments = AsyncMock(return_value={"requeued": 12, "requeue_failed": 3})
    reminders = AsyncMock(return_value=5)
    runs = AsyncMock(return_value=1)
    monkeypatch.setattr(service, "_recover_stale_attachments", attachments)
    monkeypatch.setattr(service, "_cleanup_stuck_reminders", reminders)
    monkeypatch.setattr(service, "_cleanup_stale_reminder_runs", runs)

    result = await service._run(limit=20)

    assert result["processed_count"] == 20
    attachments.assert_awaited_once()
    assert attachments.await_args.args[1] == 20
    reminders.assert_awaited_once()
    assert reminders.await_args.args[1] == 5
    runs.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_clamps_requested_budget_to_hard_ceiling(monkeypatch) -> None:
    """Manual callers cannot raise one sweep above the operational ceiling of 30."""
    service = ChatStaleJobCleanupService(uuid4())
    attachments = AsyncMock(return_value={"requeued": 30, "requeue_failed": 0})
    monkeypatch.setattr(service, "_recover_stale_attachments", attachments)
    monkeypatch.setattr(service, "_cleanup_stuck_reminders", AsyncMock(return_value=0))
    monkeypatch.setattr(service, "_cleanup_stale_reminder_runs", AsyncMock(return_value=0))

    result = await service._run(limit=100)

    assert result["processed_count"] == 30
    assert attachments.await_args.args[1] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "table_name"),
    [
        ("_cleanup_stuck_reminders", "USER_REMINDERS"),
        ("_cleanup_stale_reminder_runs", "USER_REMINDER_RUNS"),
    ],
)
async def test_reminder_cleanup_queries_enforce_remaining_budget(
    monkeypatch,
    method_name: str,
    table_name: str,
) -> None:
    """The SQL claim itself must prevent a reminder pass from exceeding its budget."""
    service = ChatStaleJobCleanupService(uuid4())
    session = _QuerySession()

    async def sessions(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    await getattr(service, method_name)(datetime.now(UTC), 7)

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert table_name in sql
    assert "LIMIT 7" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
