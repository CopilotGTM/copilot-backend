"""Shared recording-bot scheduling window policy.

Every producer eventually reaches `MeetingRecordingLifecycleService`, but producers
also create queue noise and local session rows. Keeping the window rule here lets
callers avoid work early while the lifecycle service remains the final safety gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

RECORDING_DISPATCH_WINDOW = timedelta(days=3)


def normalize_utc(value: datetime) -> datetime:
    """Normalize provider/calendar timestamps before comparing scheduling windows."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def recording_window_end(now: datetime) -> datetime:
    """Return the exclusive upper bound for automatic recording dispatch."""
    return normalize_utc(now) + RECORDING_DISPATCH_WINDOW


def is_within_recording_dispatch_window(start_at: datetime, now: datetime) -> bool:
    """Return whether a meeting can have a recording bot scheduled now."""
    start_at_utc = normalize_utc(start_at)
    now_utc = normalize_utc(now)
    return now_utc < start_at_utc < recording_window_end(now_utc)
