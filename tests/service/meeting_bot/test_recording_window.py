"""Regression tests for scheduled and ad-hoc meeting recording windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from src.service.integrations.calendar.sync_service import CalendarSyncService
from src.service.meeting_bot.lifecycle_service import MeetingRecordingLifecycleService
from src.service.meeting_bot.recording_window import is_within_recording_dispatch_window


def test_dispatch_window_includes_only_first_fifteen_minutes_after_start() -> None:
    """The late-join recovery period is bounded while future scheduling stays enabled."""
    now = datetime.now(UTC)

    assert is_within_recording_dispatch_window(now - timedelta(minutes=14), now) is True
    assert is_within_recording_dispatch_window(now - timedelta(minutes=15), now) is False
    assert is_within_recording_dispatch_window(now + timedelta(hours=1), now) is True


def test_calendar_sync_queues_meeting_discovered_inside_late_join_window() -> None:
    """A delayed calendar sync must still trigger the automatic Recall recovery path."""
    service = CalendarSyncService(AsyncMock(), uuid4())
    event = SimpleNamespace(
        event_id=uuid4(),
        start_at=datetime.now(UTC) - timedelta(minutes=2),
        conference_link="https://teams.microsoft.com/l/meetup-join/example",
        status="confirmed",
    )

    service._queue_recording_dispatch_if_eligible(event)  # noqa: SLF001

    assert service._pending_recording_event_ids == {event.event_id}  # noqa: SLF001


def test_ad_hoc_bot_matches_stable_metadata_without_provider_join_time() -> None:
    """Recall clearing ad-hoc join_at must not make a retry create a duplicate bot."""
    organisation_id = uuid4()
    event_id = uuid4()
    start_at = datetime.now(UTC) - timedelta(minutes=2)
    service = MeetingRecordingLifecycleService(organisation_id)
    dedupe_key = service._session_service.build_dedupe_key(organisation_id, event_id)  # noqa: SLF001

    matches = service._bot_matches(  # noqa: SLF001
        {
            "id": "bot_adhoc",
            "metadata": {
                "organisation_id": str(organisation_id),
                "calendar_event_id": str(event_id),
                "dedupe_key": dedupe_key,
                "scheduled_start_at": start_at.isoformat(),
            },
        },
        meeting_url="https://teams.microsoft.com/l/meetup-join/example",
        join_at=start_at,
        calendar_event_id=event_id,
        dedupe_key=dedupe_key,
        fetched_by_provider_id=True,
    )

    assert matches is True
