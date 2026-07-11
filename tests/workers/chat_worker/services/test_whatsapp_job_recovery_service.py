"""WhatsApp recovery contracts for leases, retries, and send-only resumption."""

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


def _pending_job(*, attempts: int = 1, enqueue_status: str = "failed"):
    now = datetime.now(UTC)
    return SimpleNamespace(
        job_id=uuid4(),
        status=JobStatus.PENDING,
        updated_at=now - timedelta(minutes=3),
        error_message=None,
        job_metadata={
            "enqueue_status": enqueue_status,
            "enqueue_attempts": attempts,
            "dispatch_envelope": {
                "organisation_id": str(uuid4()),
                "user_id": str(uuid4()),
                "message_text": "status?",
                "reply_context": {},
                "bot_context": {},
            },
        },
    )


def _in_progress_job(*, stage: str, age_minutes: int):
    now = datetime.now(UTC)
    return SimpleNamespace(
        job_id=uuid4(),
        chat_type="WHATSAPP_CHAT",
        status=JobStatus.IN_PROGRESS,
        updated_at=now - timedelta(minutes=age_minutes),
        error_message=None,
        job_metadata={
            "processing_stage": stage,
            "agent_success": True,
            "send_status": "retryable" if stage == "response_ready" else "not_started",
            "send_attempts": 1,
            "next_send_attempt_at": (now - timedelta(minutes=1)).isoformat(),
            "dispatch_envelope": {"reply_context": {}},
            "outbound_chunks": [{"index": 0, "text": "answer", "provider_message_id": None}],
        },
    )


@pytest.mark.asyncio
async def test_pending_recovery_uses_skip_locked_and_commits_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    service = ChatStaleJobCleanupService(organisation_id)
    job = _pending_job()
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    leased, exhausted = await service._lease_stale_whatsapp_pending_jobs(datetime.now(UTC))

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "IN ('PENDING', 'FAILED', 'ENQUEUING', 'REQUEUEING')" in sql
    assert session.commits == 1
    assert exhausted == 0
    assert leased == [(job.job_id, job.job_metadata["dispatch_envelope"], 2)]
    assert job.job_metadata["enqueue_status"] == "requeueing"
    assert job.job_metadata["enqueue_attempts"] == 2


@pytest.mark.asyncio
async def test_expired_requeue_lease_can_be_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _pending_job(attempts=2, enqueue_status="requeueing")
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    leased, exhausted = await service._lease_stale_whatsapp_pending_jobs(datetime.now(UTC))

    assert exhausted == 0
    assert leased[0][2] == 3
    assert job.job_metadata["enqueue_status"] == "requeueing"


@pytest.mark.asyncio
async def test_pending_recovery_republishes_exact_stored_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    service = ChatStaleJobCleanupService(uuid4())
    job = _pending_job(attempts=1)
    envelope = job.job_metadata["dispatch_envelope"]
    monkeypatch.setattr(
        service,
        "_lease_stale_whatsapp_pending_jobs",
        AsyncMock(return_value=([(job.job_id, envelope, 2)], 0)),
        raising=False,
    )
    record_mock = AsyncMock()
    monkeypatch.setattr(
        service,
        "_record_whatsapp_requeue_result",
        record_mock,
        raising=False,
    )
    publish_mock = AsyncMock(return_value="task-2")
    monkeypatch.setattr(
        recovery_module,
        "enqueue_whatsapp_job",
        publish_mock,
        raising=False,
    )

    counts = await service._recover_stale_whatsapp_pending_jobs(datetime.now(UTC))

    assert counts == {"requeued": 1, "requeue_failed": 0, "enqueue_exhausted": 0}
    publish_mock.assert_awaited_once_with(job_id=job.job_id, envelope=envelope)
    record_mock.assert_awaited_once_with(
        job.job_id,
        attempt=2,
        task_id="task-2",
        error=None,
    )


@pytest.mark.asyncio
async def test_pending_recovery_marks_five_attempts_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _pending_job(attempts=5)
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    leased, exhausted = await service._lease_stale_whatsapp_pending_jobs(datetime.now(UTC))

    assert leased == []
    assert exhausted == 1
    assert job.status == JobStatus.FAILED
    assert job.job_metadata["enqueue_status"] == "exhausted"
    assert "retry budget exhausted" in job.error_message.lower()


@pytest.mark.asyncio
async def test_pending_recovery_does_not_exhaust_already_published_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _pending_job(attempts=5, enqueue_status="enqueued")
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    leased, exhausted = await service._lease_stale_whatsapp_pending_jobs(datetime.now(UTC))

    assert leased == []
    assert exhausted == 0
    assert job.status == JobStatus.PENDING
    assert job.job_metadata["enqueue_status"] == "enqueued"


@pytest.mark.asyncio
async def test_stale_response_ready_job_is_leased_for_send_only_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _in_progress_job(stage="response_ready", age_minutes=3)
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    job_ids, agent_failed = await service._lease_stale_whatsapp_deliveries(datetime.now(UTC))

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "PROCESSING_STAGE" in sql
    assert "RESPONSE_READY" in sql
    assert "AGENT_RUNNING" in sql
    assert "STATUS = 'IN_PROGRESS'" in sql
    assert job_ids == [job.job_id]
    assert agent_failed == 0
    assert job.job_metadata["send_recovery_status"] == "requeueing"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_stale_agent_running_job_fails_without_agent_rerun_and_gets_static_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatStaleJobCleanupService(uuid4())
    job = _in_progress_job(stage="agent_running", age_minutes=13)
    session = _LeaseSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.stale_job_cleanup_service.acquire_org_async_session",
        sessions,
    )

    job_ids, agent_failed = await service._lease_stale_whatsapp_deliveries(datetime.now(UTC))

    assert job_ids == [job.job_id]
    assert agent_failed == 1
    assert job.status == JobStatus.FAILED
    assert job.job_metadata["agent_success"] is False
    assert job.job_metadata["processing_stage"] == "response_ready"
    assert job.job_metadata["outbound_chunks"][0]["provider_message_id"] is None
    assert "something went wrong" in job.job_metadata["outbound_chunks"][0]["text"].lower()


@pytest.mark.asyncio
async def test_stale_delivery_recovery_publishes_send_only_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    organisation_id = uuid4()
    service = ChatStaleJobCleanupService(organisation_id)
    job_id = uuid4()
    monkeypatch.setattr(
        service,
        "_lease_stale_whatsapp_deliveries",
        AsyncMock(return_value=([job_id], 0)),
        raising=False,
    )
    record_mock = AsyncMock()
    monkeypatch.setattr(
        service,
        "_record_whatsapp_send_recovery_result",
        record_mock,
        raising=False,
    )
    publish_mock = AsyncMock(return_value="resume-task-1")
    monkeypatch.setattr(
        recovery_module,
        "enqueue_whatsapp_delivery_resume",
        publish_mock,
        raising=False,
    )

    counts = await service._recover_stale_whatsapp_deliveries(datetime.now(UTC))

    assert counts == {"send_resumed": 1, "send_resume_failed": 0, "agent_failed": 0}
    publish_mock.assert_awaited_once_with(organisation_id, job_id)
    record_mock.assert_awaited_once_with(
        job_id,
        task_id="resume-task-1",
        error=None,
    )


@pytest.mark.asyncio
async def test_send_only_recovery_publish_has_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.workers.chat_worker.services import stale_job_cleanup_service as recovery_module

    async def slow_send(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "late-task"

    monkeypatch.setattr(
        recovery_module,
        "_WHATSAPP_QUEUE_PUBLISH_TIMEOUT_SECONDS",
        0.001,
    )
    monkeypatch.setattr("src.workers.chat_worker.client.send_chat_task_async", slow_send)

    with pytest.raises(TimeoutError):
        await recovery_module.enqueue_whatsapp_delivery_resume(uuid4(), uuid4())
