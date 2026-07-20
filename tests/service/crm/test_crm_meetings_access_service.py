"""Tests for top-level CRM meetings access and recording control services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.service.crm.access.meetings_access_service import (
    CALENDAR_INTEGRATION_TYPES,
    CRMMeetingRecordingControlService,
    CRMMeetingsAccessService,
    CRMMeetingsAccessServiceError,
)
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES,
)
from src.service.meeting_bot.providers import RecallProviderError


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
    """Create the control service with fake session and lifecycle boundaries."""
    service = CRMMeetingRecordingControlService.__new__(CRMMeetingRecordingControlService)
    session_service = SimpleNamespace(
        get_by_event=AsyncMock(return_value=session),
        mark_cancelled=AsyncMock(return_value=session),
        mark_provider_failure=AsyncMock(return_value=session),
    )
    lifecycle_service = SimpleNamespace(
        schedule_for_event=AsyncMock(),
        cancel_for_event=AsyncMock(
            return_value={
                "status": "cancelled",
                "calendar_event_id": str(session.calendar_event_id) if session else "",
            }
        ),
    )
    service.organisation_id = uuid4()
    service._session_service = session_service  # noqa: SLF001
    service._lifecycle_service = lifecycle_service  # noqa: SLF001
    return service, session_service, lifecycle_service


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


def test_crm_meetings_uses_provider_parent_calendar_integrations() -> None:
    assert CALENDAR_INTEGRATION_TYPES == PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES


def test_can_invite_bot_keeps_user_action_available_for_already_started_meeting() -> None:
    event = _calendar_event(start_at=datetime.now(UTC) - timedelta(minutes=10))

    assert CRMMeetingsAccessService._can_invite_bot(event, None) is True


def test_crm_initial_manual_invite_expires_after_late_join_window() -> None:
    event = _calendar_event(start_at=datetime.now(UTC) - timedelta(minutes=20))

    assert CRMMeetingsAccessService._can_invite_bot(event, None) is False


def test_never_policy_hides_manual_crm_invite() -> None:
    """The organisation privacy switch applies to user-triggered invites too."""
    event = _calendar_event(start_at=datetime.now(UTC) + timedelta(hours=1))

    assert (
        CRMMeetingsAccessService._can_invite_bot(
            event,
            None,
            policy_denial_reason="meeting_recording_policy_never",
        )
        is False
    )


@pytest.mark.asyncio
async def test_manual_crm_invite_requests_prejoin_recovery() -> None:
    """CRM manual sends may retry a terminal bot but still use central policy enforcement."""
    event_id = uuid4()
    service, _session_service, lifecycle_service = _control_service_with_session(None)
    lifecycle_service.schedule_for_event.return_value = {
        "status": "sent",
        "calendar_event_id": str(event_id),
    }

    result = await service.invite_bot(event_id)

    assert result["status"] == "sent"
    lifecycle_service.schedule_for_event.assert_awaited_once_with(
        event_id,
        source="crm_manual_invite",
        allow_prejoin_reinvite=True,
        enforce_policy=False,
    )


@pytest.mark.asyncio
async def test_cancel_bot_marks_unsent_session_cancelled() -> None:
    """Unsent sessions pass the CRM guard and delegate lifecycle cancellation."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.PENDING_SEND)
    service, session_service, lifecycle_service = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result["status"] == "cancelled"
    session_service.get_by_event.assert_awaited_once_with(event_id)
    lifecycle_service.cancel_for_event.assert_awaited_once_with(
        event_id,
        reason="manual_user_cancel",
    )


@pytest.mark.asyncio
async def test_cancel_bot_deletes_sent_scheduled_session_before_marking_cancelled() -> None:
    """Sent scheduled sessions are delegated after CRM validates removability."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.SENT, sent=True)
    service, _session_service, lifecycle_service = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result["status"] == "cancelled"
    lifecycle_service.cancel_for_event.assert_awaited_once_with(
        event_id,
        reason="manual_user_cancel",
    )


@pytest.mark.asyncio
async def test_cancel_bot_deletes_provider_id_even_when_sent_timestamp_missing() -> None:
    """Provider-id rows are still delegated even when sent timestamp is missing."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.PENDING_SEND)
    session.provider_session_id = "bot_123"
    service, _session_service, lifecycle_service = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result["status"] == "cancelled"
    lifecycle_service.cancel_for_event.assert_awaited_once_with(
        event_id,
        reason="manual_user_cancel",
    )


@pytest.mark.asyncio
async def test_cancel_bot_rejects_in_call_session_without_leave_call() -> None:
    """V1 remove semantics do not remove bots that already joined a call."""
    session = _recording_session(status=MeetingRecordingStatus.IN_CALL, sent=True)
    service, session_service, lifecycle_service = _control_service_with_session(session)

    with pytest.raises(CRMMeetingsAccessServiceError) as exc_info:
        await service.cancel_bot(uuid4())

    assert exc_info.value.status_code == 409
    lifecycle_service.cancel_for_event.assert_not_awaited()
    session_service.mark_cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_bot_rejects_transcript_ready_session() -> None:
    """Transcript-ready recordings are not removable from this API."""
    session = _recording_session(
        status=MeetingRecordingStatus.SENT,
        transcript_status=MeetingTranscriptStatus.READY,
        sent=True,
    )
    service, session_service, lifecycle_service = _control_service_with_session(session)

    with pytest.raises(CRMMeetingsAccessServiceError) as exc_info:
        await service.cancel_bot(uuid4())

    assert exc_info.value.status_code == 409
    lifecycle_service.cancel_for_event.assert_not_awaited()
    session_service.mark_cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_bot_provider_failure_preserves_user_opt_out() -> None:
    """Provider delete errors are surfaced as CRM control failures."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.SENT, sent=True)
    service, session_service, lifecycle_service = _control_service_with_session(session)
    lifecycle_service.cancel_for_event.side_effect = RecallProviderError(
        "Recall delete failed",
        status_code=500,
        code="provider_error",
        detail="temporary outage",
    )

    with pytest.raises(CRMMeetingsAccessServiceError) as exc_info:
        await service.cancel_bot(event_id)

    assert exc_info.value.status_code == 502
    lifecycle_service.cancel_for_event.assert_awaited_once_with(
        event_id,
        reason="manual_user_cancel",
    )
    session_service.mark_cancelled.assert_not_awaited()
    session_service.mark_provider_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_bot_treats_provider_404_as_cancelled() -> None:
    """A missing provider bot is already cleaned up from the user's perspective."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.SENT, sent=True)
    service, _session_service, lifecycle_service = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result["status"] == "cancelled"
    lifecycle_service.cancel_for_event.assert_awaited_once_with(
        event_id,
        reason="manual_user_cancel",
    )


@pytest.mark.asyncio
async def test_cancel_bot_retries_provider_cancel_after_previous_cancel_failure() -> None:
    """A cancelled opt-out with a provider error still retries provider cleanup."""
    event_id = uuid4()
    session = _recording_session(status=MeetingRecordingStatus.CANCELLED, sent=True)
    session.provider_payload = {"last_cancel_error": {"detail": "temporary outage"}}
    service, _session_service, lifecycle_service = _control_service_with_session(session)

    result = await service.cancel_bot(event_id)

    assert result["status"] == "cancelled"
    lifecycle_service.cancel_for_event.assert_awaited_once_with(
        event_id,
        reason="manual_user_cancel",
    )


def test_retryable_cancel_error_keeps_remove_action_available() -> None:
    """Frontend can retry provider cleanup after a previous cancel failure."""
    session = _recording_session(status=MeetingRecordingStatus.CANCELLED, sent=True)
    session.provider_payload = {"last_cancel_error": {"detail": "temporary outage"}}

    assert CRMMeetingsAccessService._can_remove_bot(session) is True


def test_format_recording_exposes_display_status_for_failed_join() -> None:
    """CRM meetings API should give frontend the same semantic failure code."""
    session = _recording_session(
        status=MeetingRecordingStatus.FAILED,
        transcript_status=MeetingTranscriptStatus.FAILED,
        sent=True,
    )
    session.status_message = "bot_kicked_from_waiting_room"

    recording = CRMMeetingsAccessService._format_recording(session)

    assert recording.status == "FAILED"
    assert recording.transcript_status == "FAILED"
    assert recording.display_status == "bot_did_not_join"


def test_visible_events_query_deduplicates_user_state_matches() -> None:
    """Multiple active calendar integrations must not duplicate the same event in pages."""
    service = CRMMeetingsAccessService(
        org_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    compiled = str(
        service._base_visible_events_stmt().compile(  # noqa: SLF001
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "SELECT DISTINCT calendar_events.event_id" in compiled
    assert "'google'" in compiled
    assert "'microsoft'" in compiled
    assert "'google_calendar'" not in compiled
    assert "'outlook_calendar'" not in compiled
