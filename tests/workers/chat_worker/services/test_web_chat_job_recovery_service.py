"""Regression tests for durable web-chat queue publication and recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.job_models import JobStatus
from src.workers.chat_worker.services.stale_job_cleanup_service import (
    ChatStaleJobCleanupService,
)


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self.values)


class _LeaseSession:
    def __init__(self, jobs):
        self.jobs = jobs
        self.statement = None
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.jobs)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


def _pending_web_job(*, attempts: int = 1, enqueue_status: str = "failed"):
    now = datetime.now(UTC)
    return SimpleNamespace(
        job_id=uuid4(),
        chat_type="GENERAL_CHAT",
        status=JobStatus.PENDING,
        updated_at=now - timedelta(minutes=3),
        error_message=None,
        job_metadata={
            "enqueue_status": enqueue_status,
            "enqueue_attempts": attempts,
        },
    )


@pytest.mark.asyncio
async def test_web_pending_recovery_leases_general_chat_job(monkeypatch) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _pending_web_job()
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    leased, exhausted = await service._lease_stale_web_chat_pending_jobs(datetime.now(UTC))

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "CHAT_TYPE = 'GENERAL_CHAT'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert leased == [(job.job_id, 2)]
    assert exhausted == 0
    assert job.job_metadata["enqueue_status"] == "requeueing"
    assert job.job_metadata["enqueue_attempts"] == 2
    assert session.commits == 1


@pytest.mark.asyncio
async def test_web_pending_recovery_reclaims_published_but_unclaimed_job(
    monkeypatch,
) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _pending_web_job(attempts=1, enqueue_status="enqueued")
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    leased, exhausted = await service._lease_stale_web_chat_pending_jobs(datetime.now(UTC))

    assert leased == [(job.job_id, 2)]
    assert exhausted == 0
    assert job.job_metadata["enqueue_status"] == "requeueing"


@pytest.mark.asyncio
async def test_web_pending_recovery_republishes_job_id(monkeypatch) -> None:
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    organisation_id = uuid4()
    service = ChatStaleJobCleanupService(organisation_id)
    job_id = uuid4()
    monkeypatch.setattr(
        service,
        "_lease_stale_web_chat_pending_jobs",
        AsyncMock(return_value=([(job_id, 2)], 0)),
        raising=False,
    )
    record_mock = AsyncMock()
    monkeypatch.setattr(
        service,
        "_record_web_chat_requeue_result",
        record_mock,
        raising=False,
    )
    publish_mock = AsyncMock(return_value="web-task-2")
    monkeypatch.setattr(
        recovery_module,
        "enqueue_web_chat_job",
        publish_mock,
        raising=False,
    )

    counts = await service._recover_stale_web_chat_pending_jobs(datetime.now(UTC))

    assert counts == {"requeued": 1, "requeue_failed": 0, "enqueue_exhausted": 0}
    publish_mock.assert_awaited_once_with(organisation_id, job_id)
    record_mock.assert_awaited_once_with(
        job_id,
        attempt=2,
        task_id="web-task-2",
        error=None,
    )


@pytest.mark.asyncio
async def test_web_pending_recovery_exhausts_retry_budget(monkeypatch) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _pending_web_job(attempts=5)
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    leased, exhausted = await service._lease_stale_web_chat_pending_jobs(datetime.now(UTC))

    assert leased == []
    assert exhausted == 1
    assert job.status == JobStatus.FAILED
    assert job.job_metadata["enqueue_status"] == "exhausted"


@pytest.mark.asyncio
async def test_web_chat_recovery_publish_has_timeout(monkeypatch) -> None:
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    async def slow_send(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "late-task"

    monkeypatch.setattr(recovery_module, "_CHAT_QUEUE_PUBLISH_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr("src.workers.chat_worker.client.send_chat_task_async", slow_send)

    with pytest.raises(TimeoutError):
        await recovery_module.enqueue_web_chat_job(uuid4(), uuid4())
