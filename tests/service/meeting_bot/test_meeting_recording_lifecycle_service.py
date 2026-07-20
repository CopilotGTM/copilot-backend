"""Tests for meeting recording lifecycle orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.service.meeting_bot.lifecycle_service as lifecycle_module
import src.service.meeting_bot.policy as policy_module
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.service.meeting_bot.lifecycle_service import MeetingRecordingLifecycleService
from src.service.meeting_bot.providers import RecallProviderError


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._value


class _FakeMasterSession:
    def __init__(self, organisation):
        self.organisation = organisation

    async def execute(self, _stmt):  # noqa: ANN001
        return _FakeScalarResult(self.organisation)


class _CapturingOrgSession:
    def __init__(self, value):
        self.value = value
        self.statement = None

    async def execute(self, stmt):  # noqa: ANN001
        self.statement = stmt
        return _FakeScalarResult(self.value)


def _yield_session(session):
    async def _gen():
        yield session

    return _gen()


@pytest.mark.asyncio
async def test_get_calendar_event_eager_loads_attendees_for_detached_policy(monkeypatch):
    """The lifecycle service evaluates attendee policy after releasing its DB session."""
    organisation_id = uuid4()
    event_id = uuid4()
    event = SimpleNamespace(event_id=event_id)
    session = _CapturingOrgSession(event)

    monkeypatch.setattr(
        lifecycle_module,
        "acquire_org_async_session",
        lambda _organisation_id: _yield_session(session),
    )

    result = await MeetingRecordingLifecycleService(organisation_id)._get_calendar_event(  # noqa: SLF001
        event_id
    )

    assert result is event
    assert session.statement is not None
    assert session.statement._with_options  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancelled_calendar_event_uses_cancel_path(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(event_id=event_id, status="cancelled")

    async def fake_get_calendar_event(calendar_event_id):
        assert calendar_event_id == event_id
        return event

    async def fake_cancel(calendar_event_id, *, reason):
        assert calendar_event_id == event_id
        assert reason == "calendar_cancelled"
        return {"status": "cancelled", "calendar_event_id": str(event_id)}

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)
    monkeypatch.setattr(service, "cancel_for_event", fake_cancel)

    result = await service.sync_event_lifecycle(event_id)

    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sync_event_lifecycle_skips_missing_event(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)

    async def fake_get_calendar_event(calendar_event_id):
        assert calendar_event_id == event_id
        return None

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)

    result = await service.sync_event_lifecycle(event_id)

    assert result == {
        "status": "skipped",
        "reason": "event_not_found",
        "calendar_event_id": str(event_id),
    }


@pytest.mark.asyncio
async def test_sync_event_lifecycle_delegates_non_cancelled_event_to_schedule(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(event_id=event_id, status="confirmed")
    calls = []

    async def fake_get_calendar_event(calendar_event_id):
        assert calendar_event_id == event_id
        return event

    async def fake_schedule(calendar_event_id, **kwargs):
        calls.append((calendar_event_id, kwargs))
        return {"status": "sent", "calendar_event_id": str(calendar_event_id)}

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)
    monkeypatch.setattr(service, "schedule_for_event", fake_schedule)

    result = await service.sync_event_lifecycle(
        event_id,
        source="unit_test",
        allow_cancelled_reinvite=True,
    )

    assert result["status"] == "sent"
    assert calls == [
        (
            event_id,
            {
                "source": "unit_test",
                "allow_cancelled_reinvite": True,
                "event": event,
            },
        )
    ]


@pytest.mark.asyncio
async def test_cancellation_does_not_check_active_calendar_integration(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    provider_session_id = "recall-bot-1"
    service = MeetingRecordingLifecycleService(organisation_id)
    session_record = SimpleNamespace(
        session_id=session_id,
        status=MeetingRecordingStatus.SENT,
        provider_session_id=provider_session_id,
        provider_payload={},
    )
    provider_calls = []
    marked = []

    async def fake_get_by_event(calendar_event_id):
        assert calendar_event_id == event_id
        return session_record

    class Provider:
        async def cancel_session(self, value):
            provider_calls.append(value)

    async def fake_build_provider():
        return Provider()

    async def fake_mark_cancelled(value, **kwargs):
        marked.append((value, kwargs))
        return session_record

    async def fail_if_called(calendar_event_id):
        raise AssertionError("active integration check must not run for cancellation")

    monkeypatch.setattr(service._session_service, "get_by_event", fake_get_by_event)
    monkeypatch.setattr(service._session_service, "mark_cancelled", fake_mark_cancelled)
    monkeypatch.setattr(service, "_build_provider", fake_build_provider)
    monkeypatch.setattr(service, "_has_active_calendar_integration", fail_if_called)

    result = await service.cancel_for_event(event_id, reason="calendar_cancelled")

    assert result["status"] == "cancelled"
    assert provider_calls == [provider_session_id]
    assert marked[0][0] == session_id


@pytest.mark.asyncio
async def test_cancel_unstarted_provider_delete_failure_does_not_leave_call(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    session_record = SimpleNamespace(
        session_id=session_id,
        status=MeetingRecordingStatus.SENT,
        provider_session_id="bot_scheduled",
        provider_payload={},
    )
    marked = []

    class Provider:
        async def cancel_session(self, _value):
            raise RecallProviderError(
                "cannot delete scheduled bot",
                status_code=400,
                code="cannot_delete_bot",
            )

        async def fetch_session(self, _value):
            return {"id": "bot_scheduled", "status": "scheduled"}

        async def leave_call(self, _value):
            raise AssertionError("unstarted bots must not receive leave_call")

    async def fake_mark_cancelled(value, **kwargs):
        marked.append((value, kwargs))
        return session_record

    monkeypatch.setattr(service._session_service, "get_by_event", AsyncGet(session_record))
    monkeypatch.setattr(service._session_service, "mark_cancelled", fake_mark_cancelled)
    monkeypatch.setattr(service, "_build_provider", AsyncGet(Provider()))

    with pytest.raises(RecallProviderError):
        await service.cancel_for_event(event_id, reason="calendar_cancelled")

    assert marked[0][0] == session_id
    assert marked[0][1]["provider_payload"]["last_cancel_error"]["operation"] == ("calendar_cancel")


@pytest.mark.asyncio
async def test_cancel_active_provider_delete_failure_uses_leave_call(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    provider_session_id = "bot_waiting"
    service = MeetingRecordingLifecycleService(organisation_id)
    session_record = SimpleNamespace(
        session_id=session_id,
        status=MeetingRecordingStatus.SENT,
        provider_session_id=provider_session_id,
        provider_status="in_waiting_room",
        provider_payload={},
    )
    provider_calls = []
    marked = []

    class Provider:
        async def cancel_session(self, value):
            provider_calls.append(("cancel", value))
            raise RecallProviderError(
                "cannot delete active bot",
                status_code=405,
                code="cannot_delete_bot",
            )

        async def fetch_session(self, _value):
            raise AssertionError("active local provider_status should avoid provider fetch")

        async def leave_call(self, value):
            provider_calls.append(("leave", value))

    async def fake_mark_cancelled(value, **kwargs):
        marked.append((value, kwargs))
        return session_record

    monkeypatch.setattr(service._session_service, "get_by_event", AsyncGet(session_record))
    monkeypatch.setattr(service._session_service, "mark_cancelled", fake_mark_cancelled)
    monkeypatch.setattr(service, "_build_provider", AsyncGet(Provider()))

    result = await service.cancel_for_event(event_id, reason="calendar_cancelled")

    assert result["status"] == "cancelled"
    assert provider_calls == [("cancel", provider_session_id), ("leave", provider_session_id)]
    assert marked[0][0] == session_id
    assert marked[0][1]["provider_payload"]["cancelled_provider_session_id"] == (
        provider_session_id
    )


@pytest.mark.asyncio
async def test_replacement_send_does_not_clear_provider_id_when_cancel_fails():
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    reset_session = SimpleNamespace(session_id=session_id)
    reset_session.__dict__["_reset_claimed"] = True
    service._session_service.reset_for_resend_if_current = AsyncMock(  # noqa: SLF001
        return_value=reset_session
    )
    session_record = SimpleNamespace(
        session_id=session_id,
        provider_session_id="bot_old",
    )
    provider = SimpleNamespace(
        cancel_session=AsyncMock(
            side_effect=RecallProviderError(
                "provider unavailable",
                status_code=503,
                code="service_unavailable",
            )
        ),
    )

    with pytest.raises(RecallProviderError):
        await service._prepare_replacement_send(  # noqa: SLF001
            provider=provider,
            session_record=session_record,
            calendar_event_id=event_id,
            meeting_url="https://meet.google.com/new-link",
            join_at=object(),
            expected_provider_session_id="bot_old",
            expected_meeting_url="https://meet.google.com/old-link",
            expected_join_at=object(),
            expected_sent_to_provider_at=object(),
        )

    service._session_service.reset_for_resend_if_current.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_dispatch_event_skips_internal_meeting_under_external_only_policy(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(
        event_id=event_id,
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        start_at=datetime.now(UTC) + timedelta(hours=2),
        attendees=[SimpleNamespace(email="seller@acme.com")],
    )
    organisation = SimpleNamespace(
        preferences={"meeting_recording": {"bot_join_policy": "external_only"}},
        domain=["acme.com"],
    )

    async def fake_get_calendar_event(calendar_event_id):
        assert calendar_event_id == event_id
        return event

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)
    monkeypatch.setattr(service, "_has_active_calendar_integration", AsyncGet(True))
    monkeypatch.setattr(
        policy_module,
        "acquire_master_async_session",
        lambda: _yield_session(_FakeMasterSession(organisation)),
    )
    monkeypatch.setattr(
        service._session_service,
        "get_or_create_pending_session",
        AsyncMock(),
    )

    result = await service.dispatch_event(event_id)

    assert result == {
        "status": "skipped",
        "reason": "meeting_recording_policy_external_only",
        "calendar_event_id": str(event_id),
    }
    service._session_service.get_or_create_pending_session.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_manual_late_join_bypasses_policy_and_dispatches_ad_hoc(monkeypatch):
    """Manual recovery bypasses external-only but still reaches the shared policy gate."""
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(
        event_id=event_id,
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        start_at=datetime.now(UTC) - timedelta(minutes=2),
        attendees=[SimpleNamespace(email="seller@acme.com")],
    )
    pending_session = SimpleNamespace(
        session_id=session_id,
        dedupe_key="meeting-recording-key",
        sent_to_provider_at=None,
    )
    queued_session = SimpleNamespace(
        session_id=session_id,
        dedupe_key="meeting-recording-key",
        sent_to_provider_at=None,
    )
    queued_session.__dict__["_send_claimed"] = True

    async def fake_get_calendar_event(calendar_event_id):
        assert calendar_event_id == event_id
        return event

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)
    monkeypatch.setattr(service, "_has_active_calendar_integration", AsyncGet(True))
    monkeypatch.setattr(
        "src.service.meeting_bot.lifecycle_service.load_meeting_recording_policy_context",
        AsyncMock(
            return_value=SimpleNamespace(bot_join_policy="external_only", internal_domains=set())
        ),
    )
    monkeypatch.setattr(
        service._session_service,
        "get_by_event",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        service._session_service,
        "get_or_create_pending_session",
        AsyncMock(return_value=pending_session),
    )
    monkeypatch.setattr(
        service._session_service,
        "mark_send_queued",
        AsyncMock(return_value=queued_session),
    )
    monkeypatch.setattr(service._session_service, "mark_sent", AsyncMock())
    provider = SimpleNamespace(
        create_session=AsyncMock(return_value={"id": "bot_123", "status": "joining_call"})
    )
    monkeypatch.setattr(
        service,
        "_build_provider",
        AsyncMock(return_value=provider),
    )
    monkeypatch.setattr(service, "_resolve_bot_name", AsyncMock(return_value="Acme Notetaker"))

    result = await service.dispatch_event(event_id, enforce_policy=False)

    assert result["status"] == "sent"
    service._session_service.get_or_create_pending_session.assert_awaited_once()  # noqa: SLF001
    assert provider.create_session.await_args.kwargs["join_at"] is None


@pytest.mark.asyncio
async def test_manual_invite_reopens_terminal_bot_that_never_entered_call(monkeypatch) -> None:
    """A user may retry one terminal waiting-room bot until the event ends."""
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    now = datetime.now(UTC)
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(
        event_id=event_id,
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        start_at=now - timedelta(minutes=20),
        end_at=now + timedelta(minutes=10),
        attendees=[],
    )
    existing = SimpleNamespace(
        session_id=session_id,
        dedupe_key="meeting-recording-key",
        meeting_url=event.conference_link,
        join_at=event.start_at,
        status=MeetingRecordingStatus.FAILED,
        transcript_status=MeetingTranscriptStatus.FAILED,
        provider_session_id="bot_old",
        provider_status="call_ended",
        provider_payload={},
        sent_to_provider_at=now - timedelta(minutes=19),
        transcript_ready_at=None,
        video_blob_path=None,
        video_artifact_id=None,
    )
    reset = SimpleNamespace(
        session_id=session_id,
        dedupe_key="meeting-recording-key",
        status=MeetingRecordingStatus.PENDING_SEND,
        sent_to_provider_at=None,
    )
    reset.__dict__["_prejoin_reset_claimed"] = True
    queued = SimpleNamespace(
        session_id=session_id,
        dedupe_key="meeting-recording-key",
        status=MeetingRecordingStatus.SEND_QUEUED,
        sent_to_provider_at=None,
    )
    queued.__dict__["_send_claimed"] = True
    provider_bot = {
        "id": "bot_old",
        "status": "done",
        "metadata": {
            "organisation_id": str(organisation_id),
            "calendar_event_id": str(event_id),
            "dedupe_key": "meeting-recording-key",
            "scheduled_start_at": event.start_at.isoformat(),
        },
        "status_changes": [
            {"code": "joining_call"},
            {"code": "in_waiting_room"},
            {"code": "call_ended", "sub_code": "bot_kicked_from_waiting_room"},
        ],
        "recordings": [],
    }
    provider = SimpleNamespace(
        fetch_session=AsyncMock(return_value=provider_bot),
        list_sessions=AsyncMock(return_value=[]),
        create_session=AsyncMock(return_value={"id": "bot_new", "status": "joining_call"}),
    )

    monkeypatch.setattr(service, "_get_calendar_event", AsyncMock(return_value=event))
    monkeypatch.setattr(service, "_has_active_calendar_integration", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lifecycle_module,
        "load_meeting_recording_policy_context",
        AsyncMock(return_value=SimpleNamespace(bot_join_policy="all", internal_domains=set())),
    )
    monkeypatch.setattr(service._session_service, "get_by_event", AsyncMock(return_value=existing))
    monkeypatch.setattr(
        service._session_service,
        "get_or_create_pending_session",
        AsyncMock(return_value=existing),
    )
    reset_mock = AsyncMock(return_value=reset)
    monkeypatch.setattr(
        service._session_service,
        "reset_prejoin_for_manual_reinvite",
        reset_mock,
    )
    monkeypatch.setattr(
        service._session_service, "mark_send_queued", AsyncMock(return_value=queued)
    )
    monkeypatch.setattr(
        service._session_service,
        "mark_sent",
        AsyncMock(return_value=SimpleNamespace(status=MeetingRecordingStatus.SENT)),
    )
    monkeypatch.setattr(service, "_build_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(service, "_resolve_bot_name", AsyncMock(return_value="Acme Notetaker"))

    result = await service.dispatch_event(
        event_id,
        allow_prejoin_reinvite=True,
        enforce_policy=False,
    )

    assert result["status"] == "reinvited"
    reset_mock.assert_awaited_once_with(
        session_id,
        expected_provider_session_id="bot_old",
        meeting_url=event.conference_link,
        join_at=event.start_at,
    )
    assert provider.create_session.await_args.kwargs["join_at"] is None


@pytest.mark.asyncio
async def test_manual_invite_does_not_reopen_bot_that_entered_call(monkeypatch) -> None:
    """Provider admission history closes the one-recording session even without artifacts."""
    organisation_id = uuid4()
    event_id = uuid4()
    now = datetime.now(UTC)
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(
        event_id=event_id,
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        start_at=now - timedelta(minutes=5),
        end_at=now + timedelta(minutes=25),
        attendees=[],
    )
    existing = SimpleNamespace(
        dedupe_key="meeting-recording-key",
        meeting_url=event.conference_link,
        join_at=event.start_at,
        status=MeetingRecordingStatus.FAILED,
        transcript_status=MeetingTranscriptStatus.FAILED,
        provider_session_id="bot_old",
        provider_payload={},
        sent_to_provider_at=now - timedelta(minutes=4),
        transcript_ready_at=None,
        video_blob_path=None,
        video_artifact_id=None,
    )
    provider = SimpleNamespace(
        fetch_session=AsyncMock(
            return_value={
                "id": "bot_old",
                "status": "done",
                "metadata": {
                    "organisation_id": str(organisation_id),
                    "calendar_event_id": str(event_id),
                    "dedupe_key": "meeting-recording-key",
                    "scheduled_start_at": event.start_at.isoformat(),
                },
                "status_changes": [
                    {"code": "in_waiting_room"},
                    {"code": "in_call_not_recording"},
                    {"code": "call_ended"},
                ],
                "recordings": [],
            }
        )
    )
    monkeypatch.setattr(service, "_get_calendar_event", AsyncMock(return_value=event))
    monkeypatch.setattr(service, "_has_active_calendar_integration", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lifecycle_module,
        "load_meeting_recording_policy_context",
        AsyncMock(return_value=SimpleNamespace(bot_join_policy="all", internal_domains=set())),
    )
    monkeypatch.setattr(service._session_service, "get_by_event", AsyncMock(return_value=existing))
    reset_mock = AsyncMock()
    monkeypatch.setattr(
        service._session_service,
        "reset_prejoin_for_manual_reinvite",
        reset_mock,
    )
    monkeypatch.setattr(service, "_build_provider", AsyncMock(return_value=provider))

    result = await service.dispatch_event(
        event_id,
        allow_prejoin_reinvite=True,
        enforce_policy=False,
    )

    assert result["status"] == "already_sent"
    reset_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_dispatch_never_replaces_terminal_bot(monkeypatch) -> None:
    """Terminal outcomes remain manual-only even inside the late-join window."""
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(uuid4())
    event = SimpleNamespace(
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        start_at=datetime.now(UTC) - timedelta(minutes=5),
        end_at=datetime.now(UTC) + timedelta(minutes=25),
        attendees=[],
    )
    existing = SimpleNamespace(
        status=MeetingRecordingStatus.FAILED,
        provider_payload={},
        sent_to_provider_at=datetime.now(UTC) - timedelta(minutes=4),
    )
    monkeypatch.setattr(service, "_get_calendar_event", AsyncMock(return_value=event))
    monkeypatch.setattr(service, "_has_active_calendar_integration", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lifecycle_module,
        "load_meeting_recording_policy_context",
        AsyncMock(return_value=SimpleNamespace(bot_join_policy="all", internal_domains=set())),
    )
    monkeypatch.setattr(service._session_service, "get_by_event", AsyncMock(return_value=existing))
    build_provider = AsyncMock()
    monkeypatch.setattr(service, "_build_provider", build_provider)

    result = await service.dispatch_event(event_id)

    assert result["reason"] == "terminal_bot_requires_manual_action"
    build_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_never_policy_blocks_manual_invite_before_provider_work(monkeypatch) -> None:
    """The org privacy switch cannot be bypassed by a manual lifecycle caller."""
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(uuid4())
    event = SimpleNamespace(
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        start_at=datetime.now(UTC) + timedelta(hours=1),
        end_at=datetime.now(UTC) + timedelta(hours=2),
        attendees=[],
    )
    monkeypatch.setattr(service, "_get_calendar_event", AsyncMock(return_value=event))
    monkeypatch.setattr(service, "_has_active_calendar_integration", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lifecycle_module,
        "load_meeting_recording_policy_context",
        AsyncMock(return_value=SimpleNamespace(bot_join_policy="never", internal_domains=set())),
    )
    create_intent = AsyncMock()
    monkeypatch.setattr(service._session_service, "get_or_create_pending_session", create_intent)

    result = await service.dispatch_event(event_id, enforce_policy=False)

    assert result["reason"] == "meeting_recording_policy_never"
    create_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_event_allows_external_meeting_under_external_only_policy(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(
        event_id=event_id,
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        start_at=datetime.now(UTC) + timedelta(hours=2),
        attendees=[SimpleNamespace(email="buyer@example.com")],
    )
    organisation = SimpleNamespace(
        preferences={"meeting_recording": {"bot_join_policy": "external_only"}},
        domain=["acme.com"],
    )
    pending_session = SimpleNamespace(
        session_id=session_id,
        dedupe_key="meeting-recording-key",
        sent_to_provider_at=None,
    )
    queued_session = SimpleNamespace(
        session_id=session_id,
        dedupe_key="meeting-recording-key",
        sent_to_provider_at=None,
    )
    queued_session.__dict__["_send_claimed"] = True

    async def fake_get_calendar_event(calendar_event_id):
        assert calendar_event_id == event_id
        return event

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)
    monkeypatch.setattr(service, "_has_active_calendar_integration", AsyncGet(True))
    monkeypatch.setattr(
        policy_module,
        "acquire_master_async_session",
        lambda: _yield_session(_FakeMasterSession(organisation)),
    )
    monkeypatch.setattr(
        service._session_service,
        "get_or_create_pending_session",
        AsyncMock(return_value=pending_session),
    )
    monkeypatch.setattr(
        service._session_service,
        "mark_send_queued",
        AsyncMock(return_value=queued_session),
    )
    monkeypatch.setattr(service._session_service, "mark_sent", AsyncMock())
    monkeypatch.setattr(
        service,
        "_build_provider",
        AsyncMock(
            return_value=SimpleNamespace(
                create_session=AsyncMock(return_value={"id": "bot_123", "status": "scheduled"})
            )
        ),
    )
    monkeypatch.setattr(service, "_resolve_bot_name", AsyncMock(return_value="Acme Notetaker"))

    result = await service.dispatch_event(event_id)

    assert result["status"] == "sent"
    service._session_service.get_or_create_pending_session.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_stale_send_reconciliation_persists_terminal_bot_without_recreating() -> None:
    """A worker crash after Recall create adopts its terminal bot and stops."""
    organisation_id = uuid4()
    event_id = uuid4()
    join_at = datetime.now(UTC) - timedelta(minutes=2)
    service = MeetingRecordingLifecycleService(organisation_id)
    session_record = SimpleNamespace(
        session_id=uuid4(),
        dedupe_key="meeting-recording-key",
    )
    terminal_bot = {
        "id": "bot_created_before_crash",
        "status": "done",
        "metadata": {
            "organisation_id": str(organisation_id),
            "calendar_event_id": str(event_id),
            "dedupe_key": session_record.dedupe_key,
            "scheduled_start_at": join_at.isoformat(),
        },
        "status_changes": [
            {"code": "joining_call"},
            {"code": "in_waiting_room"},
            {"code": "fatal", "sub_code": "timeout_exceeded_waiting_room"},
        ],
        "recordings": [],
    }
    provider = SimpleNamespace(
        list_sessions=AsyncMock(return_value=[terminal_bot]),
        create_session=AsyncMock(),
    )
    update_registration = AsyncMock()
    service._session_service.update_provider_registration = update_registration  # type: ignore[method-assign]  # noqa: SLF001

    payload, adopted = await service._create_or_adopt_provider_session(  # noqa: SLF001
        provider=provider,
        session_record=session_record,
        calendar_event_id=event_id,
        meeting_url="https://meet.google.com/abc-defg-hij",
        join_at=join_at,
        join_immediately=True,
        attempt_adopt_before_create=True,
    )

    assert adopted is True
    assert payload is terminal_bot
    provider.create_session.assert_not_awaited()
    assert update_registration.await_args.kwargs["status"] == MeetingRecordingStatus.FAILED
    assert (
        update_registration.await_args.kwargs["transcript_status"] == MeetingTranscriptStatus.FAILED
    )


class AsyncGet:
    def __init__(self, value):
        self.value = value

    async def __call__(self, *_args, **_kwargs):
        return self.value
