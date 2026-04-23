"""Tests for meeting recording dispatch behavior in DealMeetingMatcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)


class _FakeExecuteResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.statements: list[str] = []

    async def execute(self, stmt):  # noqa: ANN001
        # Capture rendered SQL so tests can assert the matcher touched the
        # recording-session table without booting the ORM stack.
        self.statements.append(str(stmt))
        return _FakeExecuteResult(self.rowcount)


class TestMeetingRecordingQueueing:
    @pytest.mark.asyncio
    async def test_future_meeting_with_join_url_persists_pending_session(self) -> None:
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
    async def test_past_meeting_does_not_queue_recording(self) -> None:
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=1)
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is False
        assert matcher._pending_recording_event_ids == set()  # noqa: SLF001
        assert session.statements == []

    @pytest.mark.asyncio
    async def test_duplicate_pending_session_is_not_queued_twice(self) -> None:
        matcher = DealMeetingMatcher(uuid4())
        session = _FakeSession(rowcount=0)
        event = SimpleNamespace(
            event_id=uuid4(),
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) + timedelta(hours=1),
        )

        created = await matcher._enqueue_meeting_recording(session, event)  # noqa: SLF001

        assert created is False
        assert matcher._pending_recording_event_ids == set()  # noqa: SLF001
        assert any("meeting_recording_sessions" in stmt for stmt in session.statements)


class TestMeetingRecordingDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_pending_recordings_enqueues_one_task_per_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
