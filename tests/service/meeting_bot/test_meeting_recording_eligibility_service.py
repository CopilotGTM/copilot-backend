"""Tests for meeting recording scheduling eligibility rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from src.service.meeting_bot.eligibility_service import MeetingRecordingEligibilityService


def _event(**overrides):
    now = datetime.now(UTC)
    data = {
        "event_id": "event-1",
        "conference_link": "https://meet.example/call",
        "start_at": now + timedelta(hours=1),
        "status": "confirmed",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (None, "event_not_found"),
        (_event(status="cancelled"), "calendar_event_cancelled"),
        (_event(conference_link=None), "missing_meeting_url"),
        (_event(start_at=None), "missing_start_at"),
        (_event(start_at=datetime.now(UTC) - timedelta(minutes=1)), "meeting_already_started"),
        (
            _event(start_at=datetime.now(UTC) + timedelta(days=4)),
            "meeting_outside_recording_window",
        ),
    ],
)
def test_scheduling_ineligible_reasons(event, reason):
    service = MeetingRecordingEligibilityService()

    result = service.evaluate_for_scheduling(event, active_calendar_integration=True)

    assert result.allowed is False
    assert result.reason == reason


def test_event_state_takes_precedence_over_inactive_integration():
    service = MeetingRecordingEligibilityService()

    result = service.evaluate_for_scheduling(
        _event(status="cancelled"),
        active_calendar_integration=False,
    )

    assert result.allowed is False
    assert result.reason == "calendar_event_cancelled"


def test_scheduling_requires_active_calendar_integration_for_automatic_flow():
    service = MeetingRecordingEligibilityService()

    result = service.evaluate_for_scheduling(
        _event(),
        active_calendar_integration=False,
    )

    assert result.allowed is False
    assert result.reason == "calendar_integration_not_active"


def test_manual_reinvite_can_skip_calendar_integration_check():
    service = MeetingRecordingEligibilityService()

    result = service.evaluate_for_scheduling(
        _event(),
        active_calendar_integration=False,
        require_active_calendar_integration=False,
    )

    assert result.allowed is True
    assert result.reason is None


def test_naive_datetimes_are_treated_as_utc():
    service = MeetingRecordingEligibilityService()
    now = datetime(2026, 7, 6, 12, 0, 0)

    result = service.evaluate_for_scheduling(
        _event(start_at=datetime(2026, 7, 6, 13, 0, 0)),
        active_calendar_integration=True,
        now=now,
    )

    assert result.allowed is True
    assert result.reason is None
