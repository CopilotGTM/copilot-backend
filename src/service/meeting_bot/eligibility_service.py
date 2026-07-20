"""Scheduling eligibility rules for meeting recording bots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.service.meeting_bot.recording_window import is_within_recording_dispatch_window


@dataclass(frozen=True)
class MeetingRecordingEligibilityResult:
    """Result of checking whether a meeting can schedule a bot."""

    allowed: bool
    reason: str | None = None


class MeetingRecordingEligibilityService:
    """Evaluates whether a calendar event may schedule a recording bot."""

    def evaluate_for_scheduling(
        self,
        event: Any | None,
        *,
        active_calendar_integration: bool,
        require_active_calendar_integration: bool = True,
        now: datetime | None = None,
    ) -> MeetingRecordingEligibilityResult:
        """Evaluate shared calendar, integration, and dispatch-window requirements."""
        if event is None:
            return MeetingRecordingEligibilityResult(False, "event_not_found")

        if (getattr(event, "status", None) or "").lower() == "cancelled":
            return MeetingRecordingEligibilityResult(False, "calendar_event_cancelled")

        meeting_url = getattr(event, "conference_link", None)
        if not meeting_url:
            return MeetingRecordingEligibilityResult(False, "missing_meeting_url")

        join_at = getattr(event, "start_at", None)
        if join_at is None:
            return MeetingRecordingEligibilityResult(False, "missing_start_at")
        if join_at.tzinfo is None:
            join_at = join_at.replace(tzinfo=UTC)

        if require_active_calendar_integration and not active_calendar_integration:
            return MeetingRecordingEligibilityResult(False, "calendar_integration_not_active")

        now_utc = now or datetime.now(UTC)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)

        if not is_within_recording_dispatch_window(join_at, now_utc):
            if join_at <= now_utc:
                return MeetingRecordingEligibilityResult(
                    False, "meeting_late_join_window_elapsed"
                )
            return MeetingRecordingEligibilityResult(False, "meeting_outside_recording_window")

        return MeetingRecordingEligibilityResult(True)
