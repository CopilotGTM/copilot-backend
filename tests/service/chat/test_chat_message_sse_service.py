"""Ownership contracts for transient chat job event streams."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import JobStatus
from src.service.chat import chat_message_sse_service as sse_module
from src.service.chat.chat_message_sse_service import ChatMessageSSEService


def _patch_job_lookup(monkeypatch, job) -> None:
    if job is not None:
        job.result_data = getattr(job, "result_data", None)
        job.error_message = getattr(job, "error_message", None)
    session = AsyncMock()
    session.get.return_value = job

    async def sessions(_organisation_id):
        """Yield the controlled database response for one ownership lookup."""
        yield session

    monkeypatch.setattr(sse_module, "acquire_org_async_session", sessions)


@pytest.mark.asyncio
async def test_can_stream_rejects_non_owner(monkeypatch) -> None:
    """Only the job owner may consume transient model frames."""
    service = ChatMessageSSEService(uuid4(), uuid4(), uuid4())
    _patch_job_lookup(monkeypatch, SimpleNamespace(user_id=uuid4(), status=JobStatus.PENDING))

    assert await service.can_stream() is False


@pytest.mark.asyncio
async def test_can_stream_accepts_owner(monkeypatch) -> None:
    """A matching job owner retains the existing SSE behavior."""
    user_id = uuid4()
    service = ChatMessageSSEService(uuid4(), uuid4(), user_id)
    _patch_job_lookup(monkeypatch, SimpleNamespace(user_id=user_id, status=JobStatus.PENDING))

    assert await service.can_stream() is True


@pytest.mark.asyncio
async def test_can_stream_rejects_missing_job(monkeypatch) -> None:
    """Unknown job IDs remain indistinguishable from another user's jobs."""
    service = ChatMessageSSEService(uuid4(), uuid4(), uuid4())
    _patch_job_lookup(monkeypatch, None)

    assert await service.can_stream() is False


@pytest.mark.asyncio
async def test_completed_status_uses_owned_job_lookup(monkeypatch) -> None:
    """Completion checks must not treat another user's job as streamable."""
    user_id = uuid4()
    service = ChatMessageSSEService(uuid4(), uuid4(), user_id)
    _patch_job_lookup(monkeypatch, SimpleNamespace(user_id=user_id, status=JobStatus.COMPLETED))

    assert await service._is_job_completed() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "stored_error"),
    [
        (JobStatus.FAILED, "Agent execution failed"),
        (JobStatus.CANCELLED, None),
    ],
)
async def test_terminal_job_reports_error_without_opening_live_stream(
    monkeypatch,
    status: JobStatus,
    stored_error: str | None,
) -> None:
    user_id = uuid4()
    service = ChatMessageSSEService(uuid4(), uuid4(), user_id)
    _patch_job_lookup(
        monkeypatch,
        SimpleNamespace(
            user_id=user_id,
            status=status,
            result_data=None,
            error_message=stored_error,
        ),
    )

    async def unexpected_live_stream(*_args, **_kwargs):
        yield {"event": "unexpected", "data": "{}"}

    monkeypatch.setattr(service, "_stream_with_consumer_group", unexpected_live_stream)

    events = [event async for event in service.stream_events()]

    assert [event["event"] for event in events] == ["ready", "error", "complete"]
    payload = json.loads(events[1]["data"])
    assert payload["reconnect"] is False
    if stored_error:
        assert payload["message"] == stored_error


@pytest.mark.asyncio
async def test_stream_events_replays_after_last_event_id_by_default(monkeypatch) -> None:
    user_id = uuid4()
    service = ChatMessageSSEService(uuid4(), uuid4(), user_id)
    _patch_job_lookup(
        monkeypatch,
        SimpleNamespace(
            user_id=user_id,
            status=JobStatus.IN_PROGRESS,
            result_data=None,
            error_message=None,
        ),
    )

    class FakeStreams:
        def __init__(self) -> None:
            self.last_ids: list[str] = []

        async def xread(self, _stream_key, last_id, **_kwargs):
            self.last_ids.append(last_id)
            return [("2-0", {"type": "finalize"})]

        async def close(self) -> None:
            return None

    fake_streams = FakeStreams()
    monkeypatch.setattr(sse_module, "AsyncRedisStreams", lambda: fake_streams)

    events = [
        event
        async for event in service.stream_events(
            resume_from_id="1-0",
            timeout_seconds=1,
        )
    ]

    assert fake_streams.last_ids == ["1-0"]
    assert [event["event"] for event in events] == ["ready", "frame", "complete"]
