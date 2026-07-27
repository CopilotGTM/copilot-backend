"""Tests for simple calendar cursor metadata helpers."""

from __future__ import annotations

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations import calendar_cursor_metadata
from src.service.integrations.calendar_cursor_metadata import (
    CALENDAR_DELTA_LINK_KEY,
    get_calendar_delta_link,
    set_calendar_delta_link,
)


def test_missing_cursor_error_is_a_runtime_failure() -> None:
    """Workers can distinguish the non-retryable cursor contract failure."""
    assert issubclass(calendar_cursor_metadata.CalendarCursorMissingError, RuntimeError)


def test_get_calendar_delta_link_reads_primary_key() -> None:
    """Both providers read the canonical capability-state cursor key."""
    metadata = {CALENDAR_DELTA_LINK_KEY: "cursor-new"}
    assert get_calendar_delta_link(metadata, IntegrationType.GOOGLE_CALENDAR) == "cursor-new"


def test_get_calendar_delta_link_falls_back_to_outlook_legacy_key() -> None:
    """Queued legacy Outlook work remains readable during the state-table rollout."""
    metadata = {"outlook_delta_link": "delta-legacy"}
    assert get_calendar_delta_link(metadata, IntegrationType.OUTLOOK_CALENDAR) == "delta-legacy"


def test_set_calendar_delta_link_writes_outlook_primary_and_legacy_keys() -> None:
    """Outlook dual-writes until legacy workers have fully drained."""
    updated = set_calendar_delta_link({}, IntegrationType.OUTLOOK_CALENDAR, "delta-123")
    assert updated["outlook_delta_link"] == "delta-123"
    assert updated[CALENDAR_DELTA_LINK_KEY] == "delta-123"


def test_set_calendar_delta_link_clears_outlook_primary_and_legacy_keys() -> None:
    """Resetting Outlook removes both canonical and compatibility cursors."""
    metadata = {
        "outlook_delta_link": "delta-old",
        CALENDAR_DELTA_LINK_KEY: "delta-old",
    }
    updated = set_calendar_delta_link(metadata, IntegrationType.OUTLOOK_CALENDAR, None)
    assert "outlook_delta_link" not in updated
    assert CALENDAR_DELTA_LINK_KEY not in updated


def test_set_calendar_delta_link_writes_google_primary_key_only() -> None:
    """Google Calendar stores only its canonical capability-state token."""
    updated = set_calendar_delta_link({}, IntegrationType.GOOGLE_CALENDAR, "sync-123")
    assert updated[CALENDAR_DELTA_LINK_KEY] == "sync-123"
    assert "outlook_delta_link" not in updated
    assert get_calendar_delta_link(updated, IntegrationType.GOOGLE_CALENDAR) == "sync-123"


def test_set_calendar_delta_link_clear_removes_google_primary_key_only() -> None:
    """Resetting Google cannot alter an unrelated Outlook compatibility value."""
    metadata = {
        CALENDAR_DELTA_LINK_KEY: "sync-old",
        "outlook_delta_link": "delta-legacy",
    }
    updated = set_calendar_delta_link(metadata, IntegrationType.GOOGLE_CALENDAR, None)
    assert get_calendar_delta_link(updated, IntegrationType.GOOGLE_CALENDAR) is None
    # Keep Outlook legacy key untouched for non-Outlook clear operations.
    assert updated["outlook_delta_link"] == "delta-legacy"
