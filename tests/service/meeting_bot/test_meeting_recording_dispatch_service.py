"""Tests for meeting recording dispatch orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.domain.models.organisation_tables.sync.meeting_recording import MeetingRecordingStatus
from src.service.integrations.meeting_recording.providers import RecallProviderError
from src.service.meeting_bot.dispatch_service import (
    ACTIVE_CALENDAR_INTEGRATION_TYPES,
    MeetingRecordingDispatchService,
)


class _FakeScalarResult:
    def __init__(self, event):
        self._event = event

    def scalar_one_or_none(self):  # noqa: ANN201
        """Return the single fake calendar event for the service lookup."""
        return self._event


class _FakeLookupSession:
    def __init__(self, event):
        self._event = event

    async def execute(self, _stmt):  # noqa: ANN001
        """Mimic the SQLAlchemy execute call used by `_get_calendar_event`."""
        # The service only needs scalar lookup semantics for these tests.
        return _FakeScalarResult(self._event)


class _FakeRowsResult:
    def all(self):  # noqa: ANN201
        """Return an empty fallback result set for query-shape assertions."""
        return []


class _CapturingQuerySession:
    def __init__(self) -> None:
        self.sql = ""

    async def execute(self, stmt):  # noqa: ANN001
        """Capture compiled SQL without requiring a live PostgreSQL connection."""
        self.sql = str(
            stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": False},
            )
        )
        return _FakeRowsResult()


def _yield_session(session):
    async def _gen():
        yield session

    return _gen()


class TestMeetingRecordingDispatchService:
    """Regression coverage for meeting recording dispatch decisions."""

    @pytest.fixture(autouse=True)
    def _default_master_org_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep dispatch unit tests isolated from the real master database."""
        master_session = _FakeLookupSession(None)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_master_async_session",
            lambda: _yield_session(master_session),
        )

    def test_active_calendar_integration_types_include_parent_and_capability_rows(self) -> None:
        """Calendar sync can write user-state rows through either integration shape."""
        assert set(ACTIVE_CALENDAR_INTEGRATION_TYPES) == {
            IntegrationType.GOOGLE,
            IntegrationType.MICROSOFT,
            IntegrationType.GOOGLE_CALENDAR,
            IntegrationType.OUTLOOK_CALENDAR,
        }

    @pytest.mark.asyncio
    async def test_dispatch_event_sends_pending_session_to_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh eligible event creates exactly one provider bot session."""
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
        service._resolve_bot_name = AsyncMock(return_value="Acme Corp Notetaker")  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result["status"] == "sent"
        assert result["calendar_event_id"] == str(event_id)
        service._session_service.get_or_create_pending_session.assert_awaited_once()  # noqa: SLF001
        service._session_service.mark_send_queued.assert_awaited_once()  # noqa: SLF001
        service._session_service.mark_sent.assert_awaited_once()  # noqa: SLF001
        provider.create_session.assert_awaited_once()
        assert provider.create_session.await_args.kwargs["bot_name"] == "Acme Corp Notetaker"

    @pytest.mark.asyncio
    async def test_resolve_bot_name_uses_organisation_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recall bot joins as the tenant company not the CopilotGTM brand."""
        organisation_id = uuid4()
        master_session = _FakeLookupSession("Acme Corp")
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_master_async_session",
            lambda: _yield_session(master_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)

        assert await service._resolve_bot_name() == "Acme Corp Notetaker"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_resolve_bot_name_falls_back_to_notetaker_for_blank_org_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank or unavailable organisation names should not expose fallback branding."""
        organisation_id = uuid4()
        master_session = _FakeLookupSession("   ")
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_master_async_session",
            lambda: _yield_session(master_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)

        assert await service._resolve_bot_name() == "Notetaker"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_skips_when_join_url_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Events without a conference link are skipped before session creation."""
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
    async def test_dispatch_event_cancels_provider_bot_for_cancelled_calendar_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Webhook cancellation should reconcile the scheduled Recall bot."""
        organisation_id = uuid4()
        event_id = uuid4()
        session_id = uuid4()
        event = SimpleNamespace(
            event_id=event_id,
            conference_link=None,
            start_at=datetime.now(UTC) + timedelta(hours=2),
            status="cancelled",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)
        service._has_active_calendar_integration = AsyncMock(return_value=False)  # noqa: SLF001
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_by_event=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    provider_session_id="bot_123",
                    provider_payload={},
                    status=MeetingRecordingStatus.SENT,
                )
            ),
            mark_cancelled=AsyncMock(),
        )
        provider = SimpleNamespace(cancel_session=AsyncMock())
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "cancelled",
            "calendar_event_id": str(event_id),
        }
        service._has_active_calendar_integration.assert_not_awaited()  # noqa: SLF001
        provider.cancel_session.assert_awaited_once_with("bot_123")
        service._session_service.mark_cancelled.assert_awaited_once_with(  # noqa: SLF001
            session_id,
            status_message="Recording bot cancelled because the calendar event was cancelled",
            provider_payload={
                "last_cancel_error": None,
                "cancelled_provider_session_id": "bot_123",
            },
        )

    @pytest.mark.asyncio
    async def test_dispatch_event_requires_active_calendar_integration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale direct task cannot send a bot after integration ownership is gone."""
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
        service._has_active_calendar_integration = AsyncMock(return_value=False)  # noqa: SLF001
        session_id = uuid4()
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
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

        assert result == {
            "status": "skipped",
            "reason": "calendar_integration_not_active",
            "calendar_event_id": str(event_id),
        }
        service._session_service.get_or_create_pending_session.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_rechecks_active_integration_before_provider_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integration deactivation during dispatch must stop before provider I/O."""
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

        session_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)
        service._has_active_calendar_integration = AsyncMock(  # noqa: SLF001
            side_effect=[True, False]
        )
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key="meeting-recording-key",
                    status=MeetingRecordingStatus.PENDING_SEND,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        service._build_provider = AsyncMock()  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "calendar_integration_not_active",
            "calendar_event_id": str(event_id),
        }
        assert service._has_active_calendar_integration.await_count == 2  # noqa: SLF001
        service._session_service.mark_send_queued.assert_not_awaited()  # noqa: SLF001
        service._build_provider.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_skips_cancelled_recording_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Manual cancellation is a durable opt-out for later direct dispatch attempts."""
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
                    status=MeetingRecordingStatus.CANCELLED,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        service._build_provider = AsyncMock()  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "recording_cancelled",
            "calendar_event_id": str(event_id),
        }
        service._session_service.mark_send_queued.assert_not_awaited()  # noqa: SLF001
        service._build_provider.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_reopens_cancelled_session_for_manual_reinvite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only explicit user re-invite can reset a durable cancellation."""
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

        session_id = uuid4()
        cancelled = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            status=MeetingRecordingStatus.CANCELLED,
            provider_payload={},
            sent_to_provider_at=None,
        )
        reset = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            status=MeetingRecordingStatus.PENDING_SEND,
            sent_to_provider_at=None,
        )
        queued = SimpleNamespace(
            session_id=session_id,
            status=MeetingRecordingStatus.SEND_QUEUED,
            sent_to_provider_at=None,
            _send_claimed=True,
        )
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(return_value=cancelled),
            reset_cancelled_for_reinvite=AsyncMock(return_value=reset),
            mark_send_queued=AsyncMock(return_value=queued),
            mark_sent=AsyncMock(return_value=queued),
        )
        provider = SimpleNamespace(
            create_session=AsyncMock(return_value={"id": "bot_456", "status": "scheduled"})
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id, allow_cancelled_reinvite=True)

        assert result["status"] == "sent"
        service._session_service.reset_cancelled_for_reinvite.assert_awaited_once()  # noqa: SLF001
        provider.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_event_blocks_reinvite_until_cancel_cleanup_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A previous provider-cancel failure blocks replacement bot creation."""
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
                    status=MeetingRecordingStatus.CANCELLED,
                    provider_payload={"last_cancel_error": {"detail": "outage"}},
                    sent_to_provider_at=None,
                )
            ),
            reset_cancelled_for_reinvite=AsyncMock(),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        service._build_provider = AsyncMock()  # noqa: SLF001

        result = await service.dispatch_event(event_id, allow_cancelled_reinvite=True)

        assert result == {
            "status": "skipped",
            "reason": "cancel_cleanup_required",
            "calendar_event_id": str(event_id),
        }
        service._session_service.reset_cancelled_for_reinvite.assert_not_awaited()  # noqa: SLF001
        service._build_provider.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_stops_when_session_cancelled_before_provider_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user cancellation after pending creation prevents provider I/O."""
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

        session_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key="meeting-recording-key",
                    status=MeetingRecordingStatus.PENDING_SEND,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    status=MeetingRecordingStatus.CANCELLED,
                )
            ),
            mark_sent=AsyncMock(),
        )
        service._build_provider = AsyncMock()  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "recording_cancelled",
            "calendar_event_id": str(event_id),
        }
        service._build_provider.assert_not_awaited()  # noqa: SLF001
        service._session_service.mark_sent.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_stops_when_session_missing_before_provider_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing durable row prevents provider I/O before create."""
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

        session_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key="meeting-recording-key",
                    status=MeetingRecordingStatus.PENDING_SEND,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(return_value=None),
            mark_sent=AsyncMock(),
        )
        service._build_provider = AsyncMock()  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "recording_session_missing",
            "calendar_event_id": str(event_id),
        }
        service._build_provider.assert_not_awaited()  # noqa: SLF001
        service._session_service.mark_sent.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_does_not_call_provider_when_send_is_in_progress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A concurrent invite that loses the atomic claim cannot create a second bot."""
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

        session_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key="meeting-recording-key",
                    status=MeetingRecordingStatus.SEND_QUEUED,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    status=MeetingRecordingStatus.SEND_QUEUED,
                    sent_to_provider_at=None,
                    _send_claimed=False,
                )
            ),
            mark_sent=AsyncMock(),
        )
        service._build_provider = AsyncMock()  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "send_in_progress",
            "calendar_event_id": str(event_id),
        }
        service._build_provider.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_cancels_provider_bot_when_session_cancelled_during_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If cancellation wins during provider create, the new Recall bot is deleted."""
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

        session_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key="meeting-recording-key",
                    status=MeetingRecordingStatus.PENDING_SEND,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    status=MeetingRecordingStatus.CANCELLED,
                )
            ),
            mark_cancelled=AsyncMock(),
        )
        provider = SimpleNamespace(
            create_session=AsyncMock(return_value={"id": "bot_123", "status": "scheduled"}),
            cancel_session=AsyncMock(),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "recording_cancelled",
            "calendar_event_id": str(event_id),
        }
        provider.create_session.assert_awaited_once()
        provider.cancel_session.assert_awaited_once_with("bot_123")

    @pytest.mark.asyncio
    async def test_dispatch_event_cancels_provider_bot_when_session_missing_after_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If local persistence disappears after provider create, the provider bot is deleted."""
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

        session_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key="meeting-recording-key",
                    status=MeetingRecordingStatus.PENDING_SEND,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    status=MeetingRecordingStatus.SEND_QUEUED,
                )
            ),
            mark_sent=AsyncMock(return_value=None),
            mark_cancelled=AsyncMock(),
        )
        provider = SimpleNamespace(
            create_session=AsyncMock(return_value={"id": "bot_123", "status": "scheduled"}),
            cancel_session=AsyncMock(),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "recording_session_missing",
            "calendar_event_id": str(event_id),
        }
        provider.create_session.assert_awaited_once()
        provider.cancel_session.assert_awaited_once_with("bot_123")

    @pytest.mark.asyncio
    async def test_dispatch_event_treats_provider_404_as_cancelled_during_create_race(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A just-created provider bot that is already gone is treated as cleaned up."""
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

        session_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key="meeting-recording-key",
                    status=MeetingRecordingStatus.PENDING_SEND,
                    sent_to_provider_at=None,
                )
            ),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    status=MeetingRecordingStatus.CANCELLED,
                )
            ),
            mark_cancelled=AsyncMock(),
        )
        provider = SimpleNamespace(
            create_session=AsyncMock(return_value={"id": "bot_123", "status": "scheduled"}),
            cancel_session=AsyncMock(side_effect=RecallProviderError("missing", status_code=404)),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "skipped",
            "reason": "recording_cancelled",
            "calendar_event_id": str(event_id),
        }
        provider.cancel_session.assert_awaited_once_with("bot_123")
        service._session_service.mark_cancelled.assert_awaited_once_with(  # noqa: SLF001
            session_id,
            provider_payload={
                "last_cancel_error": None,
                "cancelled_provider_session_id": "bot_123",
            },
        )

    @pytest.mark.asyncio
    async def test_dispatch_event_returns_already_sent_when_provider_bot_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An already-sent session is reused only after Recall still matches it."""
        organisation_id = uuid4()
        event_id = uuid4()
        start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=start_at,
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
                    provider_session_id="bot_123",
                    meeting_url=event.conference_link,
                    join_at=start_at,
                )
            ),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        provider = SimpleNamespace(
            fetch_session=AsyncMock(
                return_value={
                    "id": "bot_123",
                    "meeting_url": event.conference_link,
                    "join_at": start_at.isoformat(),
                }
            )
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "already_sent",
            "calendar_event_id": str(event_id),
        }
        service._session_service.mark_send_queued.assert_not_awaited()  # noqa: SLF001
        service._session_service.mark_sent.assert_not_awaited()  # noqa: SLF001
        provider.fetch_session.assert_awaited_once_with("bot_123")

    @pytest.mark.asyncio
    async def test_dispatch_event_reuses_sent_teams_bot_when_recall_returns_structured_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teams bots are idempotent even when Recall normalizes the URL into fields."""
        organisation_id = uuid4()
        event_id = uuid4()
        start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link=(
                "https://teams.microsoft.com/l/meetup-join/"
                "19%3ameeting_example%40thread.v2/0?context=%7b%7d"
            ),
            start_at=start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        session_id = uuid4()
        dedupe_key = f"meeting-recording:{organisation_id}:{event_id}:recall_ai"
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=session_id,
                    dedupe_key=dedupe_key,
                    sent_to_provider_at=datetime.now(UTC),
                    provider_session_id="bot_teams",
                    meeting_url=event.conference_link,
                    join_at=start_at,
                )
            ),
            reset_for_resend=AsyncMock(),
            reset_for_resend_if_current=AsyncMock(),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        provider = SimpleNamespace(
            fetch_session=AsyncMock(
                return_value={
                    "id": "bot_teams",
                    "meeting_url": {
                        "platform": "microsoft_teams",
                        "tenant_id": "tenant",
                        "thread_id": "19:meeting_example@thread.v2",
                    },
                    "join_at": start_at.isoformat(),
                    "metadata": {
                        "organisation_id": str(organisation_id),
                        "calendar_event_id": str(event_id),
                        "dedupe_key": dedupe_key,
                    },
                }
            )
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "already_sent",
            "calendar_event_id": str(event_id),
        }
        service._session_service.reset_for_resend.assert_not_awaited()  # noqa: SLF001
        service._session_service.reset_for_resend_if_current.assert_not_awaited()  # noqa: SLF001
        service._session_service.mark_send_queued.assert_not_awaited()  # noqa: SLF001
        service._session_service.mark_sent.assert_not_awaited()  # noqa: SLF001
        provider.fetch_session.assert_awaited_once_with("bot_teams")

    @pytest.mark.asyncio
    async def test_dispatch_event_started_meeting_checks_existing_provider_bot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An in-progress meeting reports already_sent when the existing bot is active."""
        organisation_id = uuid4()
        event_id = uuid4()
        start_at = datetime.now(UTC) - timedelta(minutes=10)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_by_event=AsyncMock(
                return_value=SimpleNamespace(
                    session_id=uuid4(),
                    dedupe_key="meeting-recording-key",
                    sent_to_provider_at=datetime.now(UTC) - timedelta(minutes=15),
                    provider_session_id="bot_123",
                    meeting_url=event.conference_link,
                    join_at=start_at,
                )
            ),
            get_or_create_pending_session=AsyncMock(),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        provider = SimpleNamespace(
            fetch_session=AsyncMock(
                return_value={
                    "id": "bot_123",
                    "meeting_url": event.conference_link,
                    "join_at": start_at.isoformat(),
                }
            )
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "already_sent",
            "calendar_event_id": str(event_id),
        }
        service._session_service.get_or_create_pending_session.assert_not_awaited()  # noqa: SLF001
        service._session_service.mark_send_queued.assert_not_awaited()  # noqa: SLF001
        provider.fetch_session.assert_awaited_once_with("bot_123")

    @pytest.mark.asyncio
    async def test_dispatch_event_repairs_already_sent_when_provider_bot_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale local sent row is repaired instead of returned as already sent."""
        organisation_id = uuid4()
        event_id = uuid4()
        start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        session_id = uuid4()
        existing_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_missing",
            join_at=start_at,
            meeting_url=event.conference_link,
        )
        reset_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
        )
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(return_value=existing_session),
            reset_for_resend_if_current=AsyncMock(return_value=reset_session),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
            mark_provider_failure=AsyncMock(),
        )
        provider = SimpleNamespace(
            fetch_session=AsyncMock(side_effect=RecallProviderError("missing", status_code=404)),
            list_sessions=AsyncMock(return_value=[]),
            create_session=AsyncMock(return_value={"id": "bot_new", "status": "scheduled"}),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "sent",
            "calendar_event_id": str(event_id),
            "provider_session_id": "bot_new",
        }
        provider.fetch_session.assert_awaited_once_with("bot_missing")
        provider.list_sessions.assert_awaited_once()
        provider.create_session.assert_awaited_once()
        service._session_service.reset_for_resend_if_current.assert_awaited_once_with(  # noqa: SLF001
            session_id,
            expected_provider_session_id="bot_missing",
            expected_meeting_url=event.conference_link,
            expected_join_at=start_at,
            expected_sent_to_provider_at=existing_session.sent_to_provider_at,
            meeting_url=event.conference_link,
            join_at=start_at,
        )

    @pytest.mark.asyncio
    async def test_dispatch_event_cancels_mismatched_provider_bot_before_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fetched bot for another event is cleaned up before local resend."""
        organisation_id = uuid4()
        event_id = uuid4()
        start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        session_id = uuid4()
        existing_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_wrong",
            join_at=start_at,
            meeting_url=event.conference_link,
        )
        reset_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
        )
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(return_value=existing_session),
            reset_for_resend_if_current=AsyncMock(return_value=reset_session),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
            mark_provider_failure=AsyncMock(),
        )
        provider = SimpleNamespace(
            fetch_session=AsyncMock(
                return_value={
                    "id": "bot_wrong",
                    "meeting_url": event.conference_link,
                    "join_at": start_at.isoformat(),
                    "metadata": {
                        "organisation_id": str(organisation_id),
                        "calendar_event_id": str(uuid4()),
                        "dedupe_key": "other-key",
                    },
                }
            ),
            cancel_session=AsyncMock(),
            list_sessions=AsyncMock(return_value=[]),
            create_session=AsyncMock(return_value={"id": "bot_new", "status": "scheduled"}),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "sent",
            "calendar_event_id": str(event_id),
            "provider_session_id": "bot_new",
        }
        provider.cancel_session.assert_awaited_once_with("bot_wrong")
        service._session_service.reset_for_resend_if_current.assert_awaited_once_with(  # noqa: SLF001
            session_id,
            expected_provider_session_id="bot_wrong",
            expected_meeting_url=event.conference_link,
            expected_join_at=start_at,
            expected_sent_to_provider_at=existing_session.sent_to_provider_at,
            meeting_url=event.conference_link,
            join_at=start_at,
        )

    @pytest.mark.asyncio
    async def test_dispatch_event_does_not_cancel_provider_when_replacement_claim_is_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale worker must not cancel a provider bot after losing the DB claim."""
        organisation_id = uuid4()
        event_id = uuid4()
        start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        session_id = uuid4()
        existing_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_old",
            join_at=start_at,
            meeting_url=event.conference_link,
        )
        stale_current_session = SimpleNamespace(
            session_id=session_id,
            provider_session_id="bot_current",
        )
        stale_current_session.__dict__["_reset_claimed"] = False
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(return_value=existing_session),
            reset_for_resend_if_current=AsyncMock(return_value=stale_current_session),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        provider = SimpleNamespace(
            fetch_session=AsyncMock(
                return_value={
                    "id": "bot_old",
                    "meeting_url": event.conference_link,
                    "join_at": start_at.isoformat(),
                    "metadata": {
                        "organisation_id": str(organisation_id),
                        "calendar_event_id": str(uuid4()),
                        "dedupe_key": "other-key",
                    },
                }
            ),
            cancel_session=AsyncMock(),
            create_session=AsyncMock(),
            list_sessions=AsyncMock(return_value=[]),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "already_sent",
            "calendar_event_id": str(event_id),
        }
        provider.cancel_session.assert_not_awaited()
        provider.create_session.assert_not_awaited()
        service._session_service.mark_send_queued.assert_not_awaited()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dispatch_event_reschedules_with_provider_patch_when_start_time_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sent-but-moved event updates the scheduled Recall bot first."""
        organisation_id = uuid4()
        event_id = uuid4()
        new_start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=new_start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        session_id = uuid4()
        existing_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_old",
            join_at=new_start_at - timedelta(minutes=30),
            meeting_url="https://meet.google.com/abc-defg-hij",
        )
        refreshed_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
        )
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(return_value=existing_session),
            update_provider_registration=AsyncMock(return_value=refreshed_session),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
        )
        provider = SimpleNamespace(
            update_session=AsyncMock(return_value={"id": "bot_old", "status": "scheduled"}),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "rescheduled",
            "calendar_event_id": str(event_id),
            "provider_session_id": "bot_old",
        }
        service._session_service.update_provider_registration.assert_awaited_once_with(
            session_id,
            meeting_url=event.conference_link,
            join_at=new_start_at,
            provider_session_id="bot_old",
            provider_status="scheduled",
            provider_payload={"id": "bot_old", "status": "scheduled"},
            status_message="Provider session updated after calendar reschedule",
        )
        provider.update_session.assert_awaited_once()
        service._session_service.mark_send_queued.assert_not_awaited()
        service._session_service.mark_sent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_event_falls_back_to_create_when_patch_is_too_late(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recall update_bot_failed falls back to delete/create for the new target."""
        organisation_id = uuid4()
        event_id = uuid4()
        start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/changed-link",
            start_at=start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        session_id = uuid4()
        existing_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_old",
            join_at=start_at,
            meeting_url="https://meet.google.com/old-link",
        )
        refreshed_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
        )
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(return_value=existing_session),
            reset_for_resend=AsyncMock(return_value=refreshed_session),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
            mark_provider_failure=AsyncMock(),
        )
        provider = SimpleNamespace(
            update_session=AsyncMock(
                side_effect=RecallProviderError(
                    "Recall request failed: HTTP 400 update_bot_failed",
                    status_code=400,
                    code="update_bot_failed",
                    detail="Not enough time to launch new bot",
                )
            ),
            cancel_session=AsyncMock(),
            list_sessions=AsyncMock(return_value=[]),
            create_session=AsyncMock(return_value={"id": "bot_789", "status": "scheduled"}),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "rescheduled",
            "calendar_event_id": str(event_id),
            "provider_session_id": "bot_789",
        }
        provider.update_session.assert_awaited_once()
        service._session_service.reset_for_resend.assert_awaited_once_with(
            session_id,
            meeting_url=event.conference_link,
            join_at=start_at,
        )
        provider.cancel_session.assert_awaited_once_with("bot_old")
        service._session_service.mark_send_queued.assert_awaited_once_with(session_id)

    @pytest.mark.asyncio
    async def test_dispatch_event_reschedule_uses_leave_when_old_bot_cannot_be_deleted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recall cannot_delete_bot falls back to leave-call before replacement create."""
        organisation_id = uuid4()
        event_id = uuid4()
        new_start_at = datetime.now(UTC) + timedelta(hours=2)
        event = SimpleNamespace(
            event_id=event_id,
            conference_link="https://meet.google.com/abc-defg-hij",
            start_at=new_start_at,
            status="confirmed",
        )
        lookup_session = _FakeLookupSession(event)
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(lookup_session),
        )

        session_id = uuid4()
        existing_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_old",
            join_at=new_start_at - timedelta(minutes=30),
            meeting_url="https://meet.google.com/abc-defg-hij",
        )
        refreshed_session = SimpleNamespace(
            session_id=session_id,
            dedupe_key="meeting-recording-key",
        )
        service = MeetingRecordingDispatchService(organisation_id)
        service._session_service = SimpleNamespace(  # noqa: SLF001
            get_or_create_pending_session=AsyncMock(return_value=existing_session),
            reset_for_resend=AsyncMock(return_value=refreshed_session),
            mark_send_queued=AsyncMock(),
            mark_sent=AsyncMock(),
            mark_provider_failure=AsyncMock(),
        )
        provider = SimpleNamespace(
            update_session=AsyncMock(
                side_effect=RecallProviderError(
                    "Recall request failed: HTTP 400 update_bot_failed",
                    status_code=400,
                    code="update_bot_failed",
                )
            ),
            cancel_session=AsyncMock(
                side_effect=RecallProviderError(
                    "Recall delete failed: HTTP 400 cannot_delete_bot",
                    status_code=400,
                    code="cannot_delete_bot",
                )
            ),
            leave_call=AsyncMock(),
            list_sessions=AsyncMock(return_value=[]),
            create_session=AsyncMock(return_value={"id": "bot_456", "status": "scheduled"}),
        )
        service._build_provider = AsyncMock(return_value=provider)  # noqa: SLF001

        result = await service.dispatch_event(event_id)

        assert result == {
            "status": "rescheduled",
            "calendar_event_id": str(event_id),
            "provider_session_id": "bot_456",
        }
        provider.update_session.assert_awaited_once()
        provider.cancel_session.assert_awaited_once_with("bot_old")
        provider.leave_call.assert_awaited_once_with("bot_old")
        service._session_service.reset_for_resend.assert_awaited_once_with(
            session_id,
            meeting_url=event.conference_link,
            join_at=new_start_at,
        )
        provider.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_unsent_upcoming_scans_and_sends_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback scan counts normal sends, repeats, and reschedules separately."""
        organisation_id = uuid4()
        event_ids = [uuid4(), uuid4(), uuid4()]
        service = MeetingRecordingDispatchService(organisation_id)

        async def _fake_list_event_ids():
            return event_ids

        results = iter(
            [
                {"status": "sent", "calendar_event_id": str(event_ids[0])},
                {"status": "already_sent", "calendar_event_id": str(event_ids[1])},
                {"status": "rescheduled", "calendar_event_id": str(event_ids[2])},
            ]
        )

        async def _fake_dispatch_event(calendar_event_id):  # noqa: ANN001
            result = next(results)
            assert result["calendar_event_id"] == str(calendar_event_id)
            return result

        monkeypatch.setattr(service, "_list_unsent_upcoming_event_ids", _fake_list_event_ids)  # noqa: SLF001
        monkeypatch.setattr(
            service,
            "_count_upcoming_integration_meetings",
            AsyncMock(return_value=3),
        )  # noqa: SLF001
        monkeypatch.setattr(service, "dispatch_event", _fake_dispatch_event)

        result = await service.dispatch_unsent_upcoming()

        assert result == {
            "meetings_found": 3,
            "actionable_meetings_found": 3,
            "recordings_sent": 1,
            "already_sent": 1,
            "rescheduled": 1,
            "skipped": 0,
            "failed": 0,
        }

    @pytest.mark.asyncio
    async def test_dispatch_unsent_upcoming_counts_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One provider failure is isolated so the org-level fallback can continue."""
        organisation_id = uuid4()
        event_ids = [uuid4()]
        service = MeetingRecordingDispatchService(organisation_id)

        async def _fake_list_event_ids():
            return event_ids

        async def _fake_dispatch_event(_calendar_event_id):  # noqa: ANN001
            raise RuntimeError("provider temporary failure")

        monkeypatch.setattr(service, "_list_unsent_upcoming_event_ids", _fake_list_event_ids)  # noqa: SLF001
        monkeypatch.setattr(
            service,
            "_count_upcoming_integration_meetings",
            AsyncMock(return_value=1),
        )  # noqa: SLF001
        monkeypatch.setattr(service, "dispatch_event", _fake_dispatch_event)

        result = await service.dispatch_unsent_upcoming()

        assert result == {
            "meetings_found": 1,
            "actionable_meetings_found": 1,
            "recordings_sent": 0,
            "already_sent": 0,
            "rescheduled": 0,
            "skipped": 0,
            "failed": 1,
        }

    @pytest.mark.asyncio
    async def test_dispatch_unsent_upcoming_reports_total_upcoming_when_none_need_action(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logs distinguish total upcoming meetings from fallback-actionable meetings."""
        organisation_id = uuid4()
        service = MeetingRecordingDispatchService(organisation_id)

        async def _fake_list_event_ids():
            return []

        monkeypatch.setattr(service, "_list_unsent_upcoming_event_ids", _fake_list_event_ids)  # noqa: SLF001
        monkeypatch.setattr(
            service,
            "_count_upcoming_integration_meetings",
            AsyncMock(return_value=4),
        )  # noqa: SLF001
        monkeypatch.setattr(service, "dispatch_event", AsyncMock())

        result = await service.dispatch_unsent_upcoming()

        assert result == {
            "meetings_found": 4,
            "actionable_meetings_found": 0,
            "recordings_sent": 0,
            "already_sent": 0,
            "rescheduled": 0,
            "skipped": 0,
            "failed": 0,
        }
        service.dispatch_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsent_upcoming_query_selects_ordered_columns_for_postgres(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback query keeps DISTINCT ordering compatible with PostgreSQL."""
        organisation_id = uuid4()
        query_session = _CapturingQuerySession()
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(query_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)

        result = await service._list_unsent_upcoming_event_ids()  # noqa: SLF001

        assert result == []
        assert "SELECT DISTINCT calendar_events.event_id, calendar_events.start_at" in (
            query_session.sql
        )

    @pytest.mark.asyncio
    async def test_unsent_upcoming_query_excludes_cancelled_recording_sessions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback scans must not resurrect a user's cancelled recording bot."""
        organisation_id = uuid4()
        query_session = _CapturingQuerySession()
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(query_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)

        await service._list_unsent_upcoming_event_ids()  # noqa: SLF001

        assert "meeting_recording_sessions.status != " in query_session.sql

    @pytest.mark.asyncio
    async def test_unsent_upcoming_query_uses_active_calendar_integration_ownership(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback candidates come from active calendar integrations, not deal links."""
        organisation_id = uuid4()
        query_session = _CapturingQuerySession()
        monkeypatch.setattr(
            "src.service.meeting_bot.dispatch_service.acquire_org_async_session",
            lambda _org_id: _yield_session(query_session),
        )

        service = MeetingRecordingDispatchService(organisation_id)

        await service._list_unsent_upcoming_event_ids()  # noqa: SLF001

        assert "calendar_event_user_states" in query_session.sql
        assert "knowledge_integrations" in query_session.sql
        assert "calendar_event_deal_associations" not in query_session.sql
        assert "calendar_event_user_states.is_deleted IS false" in query_session.sql
        assert "knowledge_integrations.is_active IS true" in query_session.sql
        assert "knowledge_integrations.integration_type IN" in query_session.sql
        assert "calendar_events.start_at <" in query_session.sql
