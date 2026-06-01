"""Tests for top-level CRM meetings access and recording control services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.service.crm.access.meetings_access_service import (
    CRMMeetingRecordingControlService,
    CRMMeetingsAccessService,
    CRMMeetingsAccessServiceError,
)
from src.service.integrations.meeting_recording.providers import RecallProviderError


def _recording_session(
    *,
    status: MeetingRecordingStatus,
    transcript_status: MeetingTranscriptStatus = MeetingTranscriptStatus.NOT_REQUESTED,
    sent: bool = False,
) -> MeetingRecordingSession:
    """Build a minimal recording session for cancellation tests."""
    return MeetingRecordingSession(
        organisation_id=uuid4(),
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="meeting-recording-key",
        meeting_url="https://meet.example/join",
        join_at=datetime.now(UTC) + timedelta(hours=1),
        status=status,
        transcript_status=transcript_status,
        sent_to_provider_at=datetime.now(UTC) if sent else None,
        provider_session_id="bot_123" if sent else None,
    )


def _control_service_with_session(session: MeetingRecordingSession | None):
    """Create the control service with a fake dispatch/session/provider boundary."""
    service = CRMMeetingRecordingControlService.__new__(CRMMeetingRecordingControlService)
    provider = SimpleNamespace(cancel_session=AsyncMock())
    session_service = SimpleNamespace(
        get_by_event=AsyncMock(return_value=session),
        mark_cancelled=AsyncMock(return_value=session),
        mark_provider_failure=AsyncMock(return_value=session),
    )
    service.organisation_id = uuid4()
    service._dispatch_service = SimpleNamespace(  # noqa: SLF001
        _session_service=session_service,
        _build_provider=AsyncMock(return_value=provider),
    )
    return service, session_service, provider


def _calendar_event(
    *,
    start_at: datetime,
    conference_link: str | None = "https://meet.example/join",
    status: str | None = "confirmed",
) -> CalendarEvent:
    return CalendarEvent(
        organisation_id=uuid4(),
        event_id=uuid4(),
        external_event_id="provider-event-1",
        calendar_id="primary",
        title="Customer meeting",
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        conference_link=conference_link,
        status=status,
    )


def test_can_invite_bot_keeps_user_action_available_for_already_started_meeting() -> None:
    event = _calendar_event(start_at=datetime.now(UTC) - timedelta(minutes=10))

    assert CRMMeetingsAccessService._can_invite_bot(event, None) is True


@pytest.mark.asyncio
async def test_cancel_bot_marks_unsent_session_cancelled() -> None:
    """Unsent sessions are cancelled locally without provider I/O."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.PENDING_SEND)
    service, session_service, provider = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result == {"status": "cancelled", "calendar_event_id": str(event_id)}
    session_service.mark_cancelled.assert_awaited_once_with(session.session_id)
    provider.cancel_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_bot_deletes_sent_scheduled_session_before_marking_cancelled() -> None:
    """Sent scheduled sessions call Recall delete before durable cancellation."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.SENT, sent=True)
    service, session_service, provider = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result == {"status": "cancelled", "calendar_event_id": str(event_id)}
    provider.cancel_session.assert_awaited_once_with("bot_123")
    session_service.mark_cancelled.assert_awaited_once_with(
        session.session_id,
        provider_payload={
            "last_cancel_error": None,
            "cancelled_provider_session_id": "bot_123",
        },
    )


@pytest.mark.asyncio
async def test_cancel_bot_deletes_provider_id_even_when_sent_timestamp_missing() -> None:
    """Provider cleanup is keyed by provider id, even on manually repaired rows."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.PENDING_SEND)
    session.provider_session_id = "bot_123"
    service, session_service, provider = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result == {"status": "cancelled", "calendar_event_id": str(event_id)}
    provider.cancel_session.assert_awaited_once_with("bot_123")
    session_service.mark_cancelled.assert_awaited_once_with(
        session.session_id,
        provider_payload={
            "last_cancel_error": None,
            "cancelled_provider_session_id": "bot_123",
        },
    )


@pytest.mark.asyncio
async def test_cancel_bot_rejects_in_call_session_without_leave_call() -> None:
    """V1 remove semantics do not remove bots that already joined a call."""
    session = _recording_session(status=MeetingRecordingStatus.IN_CALL, sent=True)
    service, session_service, provider = _control_service_with_session(session)

    with pytest.raises(CRMMeetingsAccessServiceError) as exc_info:
        await service.cancel_bot(uuid4())

    assert exc_info.value.status_code == 409
    provider.cancel_session.assert_not_awaited()
    assert not hasattr(provider, "leave_call")
    session_service.mark_cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_bot_rejects_transcript_ready_session() -> None:
    """Transcript-ready recordings are not removable from this API."""
    session = _recording_session(
        status=MeetingRecordingStatus.SENT,
        transcript_status=MeetingTranscriptStatus.READY,
        sent=True,
    )
    service, session_service, provider = _control_service_with_session(session)

    with pytest.raises(CRMMeetingsAccessServiceError) as exc_info:
        await service.cancel_bot(uuid4())

    assert exc_info.value.status_code == 409
    provider.cancel_session.assert_not_awaited()
    session_service.mark_cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_bot_provider_failure_preserves_user_opt_out() -> None:
    """A failed Recall delete must not turn user cancellation into a resend candidate."""
    session = _recording_session(status=MeetingRecordingStatus.SENT, sent=True)
    service, session_service, provider = _control_service_with_session(session)
    provider.cancel_session.side_effect = RecallProviderError(
        "Recall delete failed",
        status_code=500,
        code="provider_error",
        detail="temporary outage",
    )

    with pytest.raises(CRMMeetingsAccessServiceError) as exc_info:
        await service.cancel_bot(uuid4())

    assert exc_info.value.status_code == 502
    provider.cancel_session.assert_awaited_once_with("bot_123")
    session_service.mark_cancelled.assert_awaited_once_with(
        session.session_id,
        status_message="Recall cancel failed: temporary outage",
        provider_payload={
            "last_cancel_error": {
                "operation": "cancel",
                "status_code": 500,
                "code": "provider_error",
                "detail": "temporary outage",
            },
        },
    )
    session_service.mark_provider_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_bot_treats_provider_404_as_cancelled() -> None:
    """A missing provider bot is already cleaned up from the user's perspective."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.SENT, sent=True)
    service, session_service, provider = _control_service_with_session(session)
    provider.cancel_session.side_effect = RecallProviderError(
        "Recall delete failed",
        status_code=404,
        code="not_found",
        detail="bot not found",
    )

    result = await service.cancel_bot(event_id)

    assert result == {"status": "cancelled", "calendar_event_id": str(event_id)}
    provider.cancel_session.assert_awaited_once_with("bot_123")
    session_service.mark_cancelled.assert_awaited_once_with(
        session.session_id,
        provider_payload={
            "last_cancel_error": None,
            "cancelled_provider_session_id": "bot_123",
        },
    )


@pytest.mark.asyncio
async def test_cancel_bot_retries_provider_cancel_after_previous_cancel_failure() -> None:
    """A cancelled opt-out with a provider error still retries provider cleanup."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.CANCELLED, sent=True)
    session.provider_payload = {"last_cancel_error": {"detail": "temporary outage"}}
    service, session_service, provider = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result == {"status": "cancelled", "calendar_event_id": str(event_id)}
    provider.cancel_session.assert_awaited_once_with("bot_123")
    session_service.mark_cancelled.assert_awaited_once_with(
        session.session_id,
        provider_payload={
            "last_cancel_error": None,
            "cancelled_provider_session_id": "bot_123",
        },
    )


def test_retryable_cancel_error_keeps_remove_action_available() -> None:
    """Frontend can retry provider cleanup after a previous cancel failure."""
    session = _recording_session(status=MeetingRecordingStatus.CANCELLED, sent=True)
    session.provider_payload = {"last_cancel_error": {"detail": "temporary outage"}}

    assert CRMMeetingsAccessService._can_remove_bot(session) is True


def test_visible_events_query_deduplicates_user_state_matches() -> None:
    """Multiple active calendar integrations must not duplicate the same event in pages."""
    service = CRMMeetingsAccessService(
        org_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    compiled = str(service._base_visible_events_stmt().compile())  # noqa: SLF001

    assert "SELECT DISTINCT calendar_events.event_id" in compiled
