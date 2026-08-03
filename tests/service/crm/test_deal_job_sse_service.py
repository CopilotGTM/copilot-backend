"""SSE contract tests for deal job visibility streams."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService


class _JobResult:
    def __init__(self, job: DealJobs) -> None:
        self.job = job

    def scalars(self):
        return self

    def first(self):
        return self.job


@pytest.mark.asyncio
async def test_stream_events_access_denied_emits_error(monkeypatch) -> None:
    """Denied jobs should emit an SSE error event and ask clients not to reconnect."""
    service = DealJobSSEService(uuid4(), uuid4(), uuid4())

    async def fake_get_job_status():
        """Make service think the job is denied and should stop stream access."""
        return None, None, True

    monkeypatch.setattr(service, "_get_job_status", fake_get_job_status)

    events: list[dict] = []
    async for event in service.stream_events():
        events.append(event)

    assert events[0]["event"] == "ready"
    assert events[1]["event"] == "error"
    payload = json.loads(events[1]["data"])
    assert payload["reconnect"] is False


@pytest.mark.asyncio
async def test_stream_events_completed_emits_cached_result(monkeypatch) -> None:
    """Completed jobs replay result plus normal finalize frame before completion."""
    service = DealJobSSEService(uuid4(), uuid4(), uuid4())
    result_data = {"created_count": 2, "failed_count": 0}

    async def fake_get_job_status():
        """Make service return cached completion payload for completed status."""
        return JobStatus.COMPLETED, result_data, False

    monkeypatch.setattr(service, "_get_job_status", fake_get_job_status)

    events: list[dict] = []
    async for event in service.stream_events():
        events.append(event)

    assert [event["event"] for event in events] == ["ready", "frame", "frame", "complete"]
    result_frame = json.loads(events[1]["data"])
    finalize_frame = json.loads(events[2]["data"])
    assert result_frame == {"type": "answer.replace", "content": result_data}
    assert finalize_frame == {"type": "finalize", "reason": "done"}


@pytest.mark.asyncio
async def test_stream_events_failed_emits_ready_error_and_complete(monkeypatch) -> None:
    """Failed jobs should close deterministically instead of leaving clients hanging."""
    service = DealJobSSEService(uuid4(), uuid4(), uuid4())

    async def fake_get_job_status():
        """Make service return a failed status with stored error metadata."""
        return JobStatus.FAILED, {"error": "research failed"}, False

    monkeypatch.setattr(service, "_get_job_status", fake_get_job_status)

    events: list[dict] = []
    async for event in service.stream_events():
        events.append(event)

    assert [event["event"] for event in events] == ["ready", "error", "complete"]
    payload = json.loads(events[1]["data"])
    assert payload["message"] == "research failed"
    assert payload["reconnect"] is False


@pytest.mark.asyncio
async def test_stream_events_finalize_retains_stream_for_replay(monkeypatch) -> None:
    """Finalize leaves the producer-TTL stream available to reconnecting clients."""
    service = DealJobSSEService(uuid4(), uuid4(), uuid4())

    async def fake_get_job_status():
        """Make service enter stream finalize branch for cleanup verification."""
        return None, None, False

    class FakeStreams:
        """Redis stream fake that emits a single finalize event then drains."""

        def __init__(self):
            self.deleted_key = None
            self._sent = False

        async def xread(self, *_args, **_kwargs):
            """Emit one finalize record, then signal no more frames."""
            if self._sent:
                return []
            self._sent = True
            return [("1-0", {"type": "finalize"})]

        async def delete_stream(self, stream_key: str):
            """Capture the deleted stream key for assertion."""
            self.deleted_key = stream_key
            return True

        async def close(self):
            """No-op close method to match stream client contract."""
            return None

    fake_streams = FakeStreams()
    monkeypatch.setattr(service, "_get_job_status", fake_get_job_status)
    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_sse_service.AsyncRedisStreams",
        lambda: fake_streams,
    )

    events: list[dict] = []
    async for event in service.stream_events(timeout_seconds=2, heartbeat_interval=0.1):
        events.append(event)

    assert events[0]["event"] == "ready"
    assert events[1]["event"] == "frame"
    assert events[2]["event"] == "complete"
    assert fake_streams.deleted_key is None


@pytest.mark.asyncio
async def test_ensure_user_access_rejects_job_for_different_deal(monkeypatch) -> None:
    """Reject access checks when requested deal does not own the SSE job."""
    organisation_id = uuid4()
    user_id = uuid4()
    requested_deal_id = uuid4()
    job = DealJobs(
        organisation_id=organisation_id,
        user_id=user_id,
        deal_id=uuid4(),
        job_type="deal_email_rematch",
    )
    service = DealJobSSEService(organisation_id, job.job_id, user_id)

    class FakeSession:
        """Async session returning a single synthetic job for access checks."""

        async def execute(self, statement):
            """Return the seeded job for access check logic."""
            assert organisation_id in statement.compile().params.values()
            return _JobResult(job)

    class FakeSessionSource:
        """Async session source that yields one fake session object."""

        async def __aiter__(self):
            """Provide exactly one fake session to match service context usage."""
            yield FakeSession()

    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_sse_service.acquire_org_async_session",
        lambda organisation_id: FakeSessionSource(),
    )

    assert await service.ensure_user_access(deal_id=requested_deal_id) is False


@pytest.mark.asyncio
async def test_deal_access_allows_collaborator_to_reuse_shared_job(monkeypatch) -> None:
    """A route-authorized collaborator can stream another user's deal-scoped job."""
    organisation_id = uuid4()
    deal_id = uuid4()
    job = DealJobs(
        organisation_id=organisation_id,
        user_id=uuid4(),
        deal_id=deal_id,
        job_type="deal_summary",
        status=JobStatus.COMPLETED,
        result_data={"summary": "shared"},
    )
    service = DealJobSSEService(organisation_id, job.job_id, uuid4())

    class FakeSession:
        """Return the shared job from the already tenant-scoped session."""

        async def execute(self, statement):
            assert organisation_id in statement.compile().params.values()
            return _JobResult(job)

    class FakeSessionSource:
        """Yield one tenant session for access and durable-state checks."""

        async def __aiter__(self):
            yield FakeSession()

    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_sse_service.acquire_org_async_session",
        lambda organisation_id: FakeSessionSource(),
    )

    assert (
        await service.ensure_deal_access(
            deal_id,
            event_id=None,
            allowed_job_types=frozenset({DealJobType.DEAL_SUMMARY}),
        )
        is True
    )
    events = [event async for event in service.stream_events()]
    assert [event["event"] for event in events] == ["ready", "frame", "frame", "complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_event_id", "allowed_job_types"),
    [
        (uuid4(), frozenset({DealJobType.MEETING_SUMMARY})),
        (None, frozenset({DealJobType.STAKEHOLDER_SYNC})),
    ],
)
async def test_deal_access_rejects_route_identity_substitution(
    monkeypatch,
    requested_event_id,
    allowed_job_types,
) -> None:
    """A deal match alone cannot authorize a different event or job type."""
    organisation_id = uuid4()
    deal_id = uuid4()
    job = DealJobs(
        organisation_id=organisation_id,
        user_id=uuid4(),
        deal_id=deal_id,
        event_id=uuid4(),
        job_type=DealJobType.MEETING_BRIEFING,
        status=JobStatus.COMPLETED,
        result_data={"briefing": "private to the requested meeting route"},
    )
    service = DealJobSSEService(organisation_id, job.job_id, uuid4())

    class FakeSession:
        async def execute(self, _statement):
            return _JobResult(job)

    class FakeSessionSource:
        async def __aiter__(self):
            yield FakeSession()

    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_sse_service.acquire_org_async_session",
        lambda organisation_id: FakeSessionSource(),
    )

    assert (
        await service.ensure_deal_access(
            deal_id,
            event_id=requested_event_id,
            allowed_job_types=allowed_job_types,
        )
        is False
    )
    _status, result, denied = await service._get_job_status()
    assert result is None
    assert denied is True
