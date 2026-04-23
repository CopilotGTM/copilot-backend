"""Tests for meeting recording dispatch orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.meeting_bot.dispatch_service import MeetingRecordingDispatchService


class _FakeScalarResult:
    def __init__(self, event):
        self._event = event

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._event


class _FakeLookupSession:
    def __init__(self, event):
        self._event = event

    async def execute(self, _stmt):  # noqa: ANN001
        # The service only needs scalar lookup semantics for these tests.
        return _FakeScalarResult(self._event)


def _yield_session(session):
    async def _gen():
        yield session

    return _gen()


class TestMeetingRecordingDispatchService:
    @pytest.mark.asyncio
    async def test_dispatch_event_sends_pending_session_to_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        organisation_id = uuid4()
        event_id = uuid4()
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) + timedelta(hours=2),
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=uuid4(),
                    dedupe_key="meeting-recording-key",
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        provider = SimpleNamespace(
            create_session=AsyncMock(return_value={"id": "bot_123", "status": "scheduled"})
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result["status"] == "sent"
        assert result["calendar_event_id"] == str(event_id)
        service._session_service.get_or_create_pending_session.assert_awaited_once()  # noqa: SLF001
        service._session_service.mark_send_queued.assert_awaited_once()  # noqa: SLF001
        service._session_service.mark_sent.assert_awaited_once()  # noqa: SLF001
        provider.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_event_skips_when_join_url_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        organisation_id = uuid4()
        event_id = uuid4()
        event = SimpleNamespace(
            event_id=event_id,
            conference_link=None,
            start_at=datetime.now(UTC) + timedelta(hours=2),
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "missing_meeting_url",
            "calendar_event_id": str(event_id),
        }
        service._session_service.get_or_create_pending_session.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_returns_already_sent_without_provider_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        organisation_id = uuid4()
        event_id = uuid4()
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=datetime.now(UTC) + timedelta(hours=2),
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=uuid4(),
                    dedupe_key="meeting-recording-key",
                    sent_to_provider_at=datetime.now(UTC),
                )
            ),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        service._build_provider = AsyncMock()  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "already_sent",
            "calendar_event_id": str(event_id),
        }
        service._session_service.mark_send_queued.assert_not_awaited()  # noqa: SLF001
        service._session_service.mark_sent.assert_not_awaited()  # noqa: SLF001
        service._build_provider.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_unsent_upcoming_scans_and_sends_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        organisation_id = uuid4()
        event_ids = [uuid4(), uuid4(), uuid4()]
        service = MeetingRecordingDispatchService(organisation_id)

        async def _fake_list_event_ids():
            return event_ids

        results = iter(
            [
                {"status": "sent", "calendar_event_id": str(event_ids[0])},
                {"status": "already_sent", "calendar_event_id": str(event_ids[1])},
                {"status": "skipped", "calendar_event_id": str(event_ids[2])},
            ]
        )

        async def _fake_dispatch_event(calendar_event_id):  # noqa: ANN001
            result = next(results)
            assert result["calendar_event_id"] == str(calendar_event_id)
            return result

        monkeypatch.setattr(service, "_list_unsent_upcoming_event_ids", _fake_list_event_ids)  # noqa: SLF001
        monkeypatch.setattr(service, "dispatch_event", _fake_dispatch_event)

        result = await service.dispatch_unsent_upcoming()

        assert result == {
            "meetings_found": 3,
            "recordings_sent": 1,
            "already_sent": 1,
            "skipped": 1,
            "failed": 0,
        }

    @pytest.mark.asyncio
    async def test_dispatch_unsent_upcoming_counts_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        organisation_id = uuid4()
        event_ids = [uuid4()]
        service = MeetingRecordingDispatchService(organisation_id)

        async def _fake_list_event_ids():
            return event_ids

        async def _fake_dispatch_event(_calendar_event_id):  # noqa: ANN001
            raise RuntimeError("provider temporary failure")

        monkeypatch.setattr(service, "_list_unsent_upcoming_event_ids", _fake_list_event_ids)  # noqa: SLF001
        monkeypatch.setattr(service, "dispatch_event", _fake_dispatch_event)

        result = await service.dispatch_unsent_upcoming()

        assert result == {
            "meetings_found": 1,
            "recordings_sent": 0,
            "already_sent": 0,
            "skipped": 0,
            "failed": 1,
        }
