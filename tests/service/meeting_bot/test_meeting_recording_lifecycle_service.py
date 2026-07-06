"""Tests for meeting recording lifecycle orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.meeting_recording import MeetingRecordingStatus
from src.service.meeting_bot.lifecycle_service import MeetingRecordingLifecycleService
from src.service.meeting_bot.providers import RecallProviderError


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


class AsyncGet:
    def __init__(self, value):
        self.value = value

    async def __call__(self, *_args, **_kwargs):
        return self.value
