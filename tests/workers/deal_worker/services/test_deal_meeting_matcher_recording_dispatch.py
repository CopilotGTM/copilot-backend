"""Tests for meeting recording dispatch behavior in DealMeetingMatcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)


class _FakeExecuteResult:
    def __init__(self, rowcount: int, scalar_result=None):
        self.rowcount = rowcount
        self._scalar_result = scalar_result

    def scalar_one_or_none(self):
        """Return the configured fake row for session lookup calls."""
        return self._scalar_result


class _FakeSession:
    def __init__(self, rowcount: int | list[int], scalar_results: list | None = None):
        self.rowcount = rowcount
        self.scalar_results = scalar_results
        self.statements: list[str] = []
        self.execute_calls = 0

    async def execute(self, stmt):  # noqa: ANN001
        """Capture SQL and return scripted rowcount/scalar results."""
        # Capture rendered SQL so tests can assert the matcher touched the
        # recording-session table without booting the ORM stack.
        self.statements.append(str(stmt))
        rowcount = self.rowcount
        if isinstance(rowcount, list):
            rowcount = rowcount[min(self.execute_calls, len(rowcount) - 1)]

        scalar_results = self.scalar_results
        scalar_result = None
        if scalar_results is not None:
            scalar_result = scalar_results[min(self.execute_calls, len(scalar_results) - 1)]

        self.execute_calls += 1
        return _FakeExecuteResult(rowcount, scalar_result)


class TestMeetingRecordingQueueing:
    """Regression coverage for matcher-side recording queue decisions."""

    @pytest.mark.asyncio
    async def test_future_meeting_with_join_url_persists_pending_session(self) -> None:
        """A future meeting with a join URL creates durable send intent."""
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=1)
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) + timedelta(hours=2),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is True
        assert event.event_id in matcher._pending_recording_event_ids  # noqa: SLF001
        assert any("meeting_recording_sessions" in stmt for stmt in session.statements)

    @pytest.mark.asyncio
    async def test_missing_join_url_does_not_persist_pending_session(self) -> None:
        """A meeting without a join URL never queues provider work."""
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=1)
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link=None,
            start_at=datetime.now(UTC) + timedelta(hours=2),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is False
        assert matcher._pending_recording_event_ids == set()  # noqa: SLF001
        assert session.statements == []

    @pytest.mark.asyncio
    async def test_recently_started_meeting_queues_ad_hoc_recording(self) -> None:
        """Matcher queues an ad-hoc bot when calendar data arrives five minutes late."""
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=1)
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is True
        assert matcher._pending_recording_event_ids == {event.event_id}  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_meeting_past_late_join_window_does_not_queue_recording(self) -> None:
        """Meetings older than 15 minutes cannot consume ad-hoc Recall capacity."""
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=1)
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) - timedelta(minutes=16),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is False
        assert matcher._pending_recording_event_ids == set()  # noqa: SLF001
        assert session.statements == []

    @pytest.mark.asyncio
    async def test_far_future_meeting_does_not_persist_pending_session(self) -> None:
        """Matcher must not create durable send intent outside the bot window."""
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=1)
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) + timedelta(days=4),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is False
        assert matcher._pending_recording_event_ids == set()  # noqa: SLF001
        assert session.statements == []

    @pytest.mark.asyncio
    async def test_duplicate_pending_session_is_not_queued_twice(self) -> None:
        """An unchanged duplicate row remains idempotent and does not requeue."""
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=[0], scalar_results=[None])
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) + timedelta(hours=1),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is False
        assert matcher._pending_recording_event_ids == set()  # noqa: SLF001
        assert any("meeting_recording_sessions" in stmt for stmt in session.statements)

    @pytest.mark.asyncio
    async def test_stale_existing_recording_session_is_requeued_for_resend(self) -> None:
        """A stale existing recording row still queues dispatch for provider resync."""
        matcher = DealMeetingMatcher(uuid4())
        event_id = uuid4()
        start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=start_at,
        )
        existing_session = MeetingRecordingSession(
            organisation_id=matcher.organisation_id,
            calendar_event_id=event_id,
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://meet.google.com/abc-defg-oldd",
            join_at=start_at + timedelta(minutes=30),
        )
        session = _FakeSession(
            rowcount=[0],
            scalar_results=[None, existing_session],
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is False
        assert matcher._pending_recording_event_ids == {event_id}  # noqa: SLF001
        assert any("select" in stmt.lower() for stmt in session.statements)


class TestMeetingRecordingDispatch:
    """Regression coverage for post-commit dispatch fanout."""

    @pytest.mark.asyncio
    async def test_dispatch_pending_recordings_enqueues_one_task_per_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Queued event ids are converted into one deal-worker task each."""
        organisation_id = uuid4()
        matcher = DealMeetingMatcher(organisation_id)
        event_ids = {uuid4(), uuid4()}
        matcher._pending_recording_event_ids = set(event_ids)  # noqa: SLF001

        dispatched: list[tuple[str, dict, str]] = []

        async def _fake_send_task_async(task_name, args=None, kwargs=None, queue="deal", **_):  # noqa: ANN001
            dispatched.append((task_name, kwargs or {}, queue))
            return "task-id"

        monkeypatch.setattr(
            "src.workers.deal_worker.services.deal_mapper_services."
            "deal_meeting_matcher.send_deal_task_async",
            _fake_send_task_async,
        )

        await matcher._dispatch_pending_recordings()  # noqa: SLF001

        assert len(dispatched) == 2
        assert {item[1]["calendar_event_id"] for item in dispatched} == {
            str(event_id) for event_id in event_ids
        }
        assert all(item[0] == "deal.send_meeting_recording" for item in dispatched)
        assert all(item[1]["organisation_id"] == str(organisation_id) for item in dispatched)
        assert matcher._pending_recording_event_ids == set()  # noqa: SLF001
