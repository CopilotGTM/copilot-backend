"""Shared recording-bot scheduling window policy.

Every producer eventually reaches `MeetingRecordingLifecycleService`, but producers
also create queue noise and local session rows. Keeping the window rule here lets
callers avoid work early while the lifecycle service remains the final safety gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

RECORDING_DISPATCH_WINDOW = timedelta(days=3)
# Recall treats bots created after the scheduled start as ad-hoc capacity. Keep
# this recovery window short so late calendar syncs can still record useful calls.
RECORDING_LATE_JOIN_WINDOW = timedelta(minutes=15)


def normalize_utc(value: datetime) -> datetime:
    """Normalize provider/calendar timestamps before comparing scheduling windows."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def recording_window_end(now: datetime) -> datetime:
    """Return the exclusive upper bound for automatic recording dispatch."""
    return normalize_utc(now) + RECORDING_DISPATCH_WINDOW


def recording_window_start(now: datetime) -> datetime:
    """Return the exclusive lower bound for ad-hoc late-join dispatch."""
    return normalize_utc(now) - RECORDING_LATE_JOIN_WINDOW


def is_within_recording_dispatch_window(start_at: datetime, now: datetime) -> bool:
    """Return whether a meeting can receive a scheduled or ad-hoc bot now."""
    start_at_utc = normalize_utc(start_at)
    now_utc = normalize_utc(now)
    return recording_window_start(now_utc) < start_at_utc < recording_window_end(now_utc)


def is_late_join_expired(start_at: datetime, now: datetime) -> bool:
    """Return whether a meeting is past the manual and automatic recovery window."""
    return normalize_utc(start_at) <= recording_window_start(now)
