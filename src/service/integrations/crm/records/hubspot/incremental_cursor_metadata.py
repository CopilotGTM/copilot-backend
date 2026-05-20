"""HubSpot CRM incremental cursor metadata helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# Re-read a small overlap on every poll because HubSpot Search is timestamp
# based, not a durable delta stream. Writers skip stale snapshots, so duplicate
# reads are cheaper than losing a late-indexed provider update.
DEFAULT_HUBSPOT_INCREMENTAL_LOOKBACK_MINUTES = 10
HUBSPOT_INCREMENTAL_RUN_TYPE = "incremental_search"


def get_hubspot_incremental_search_start(
    metadata: dict[str, Any] | None,
    object_key: str,
    *,
    default_start: datetime | None = None,
) -> datetime | None:
    """Return the timestamp HubSpot Search should use for one object family.

    Prefer the provider cursor from the previous incremental run. If this is
    the first incremental run, fall back to the same object's full-sync
    `last_synced_at`, which is now written as the full-sync start time.
    """
    record_sync = (metadata or {}).get("record_sync")
    if not isinstance(record_sync, dict):
        return default_start
    object_entry = record_sync.get(object_key)
    if not isinstance(object_entry, dict):
        return default_start

    incremental = object_entry.get("incremental")
    incremental = incremental if isinstance(incremental, dict) else {}
    lookback_minutes = _positive_int(
        incremental.get("lookback_minutes"),
        default=DEFAULT_HUBSPOT_INCREMENTAL_LOOKBACK_MINUTES,
    )

    source_cursor = _parse_datetime(incremental.get("cursor_source_modified_at"))
    if source_cursor is None:
        source_cursor = _parse_datetime(object_entry.get("last_synced_at"))
    if source_cursor is None:
        return default_start
    return source_cursor - timedelta(minutes=lookback_minutes)


def is_hubspot_incremental_ready(
    metadata: object,
    object_keys: tuple[str, ...],
) -> bool:
    """Return whether an integration has safe starting cursors for polling.

    The fan-out task uses this to avoid queuing work while initial sync is
    running; the child service repeats the same check because a task can already
    be in Redis when metadata is still partial.
    """
    if not isinstance(metadata, dict):
        return False
    initial_sync_status = metadata.get("initial_sync_status")
    if initial_sync_status is not None:
        return bool(initial_sync_status == "success")

    record_sync = metadata.get("record_sync")
    if not isinstance(record_sync, dict):
        return False

    for object_key in object_keys:
        object_entry = record_sync.get(object_key)
        if not isinstance(object_entry, dict):
            return False
        if object_entry.get("last_synced_at"):
            continue
        incremental = object_entry.get("incremental")
        if not (isinstance(incremental, dict) and incremental.get("cursor_source_modified_at")):
            return False
    return True


def set_hubspot_incremental_cursor(
    metadata: dict[str, Any] | None,
    object_key: str,
    *,
    cursor_source_modified_at: datetime | None,
    cursor_updated_at: datetime,
    seen_count: int,
    written_count: int,
    skipped_stale_count: int,
    lookback_minutes: int = DEFAULT_HUBSPOT_INCREMENTAL_LOOKBACK_MINUTES,
    run_type: str = HUBSPOT_INCREMENTAL_RUN_TYPE,
) -> dict[str, Any]:
    """Persist one object-family cursor without changing unrelated metadata.

    Passing `cursor_source_modified_at=None` intentionally keeps the previous
    provider cursor. Empty searches record run metrics, but must not advance
    the watermark to local wall-clock time.
    """
    updated = dict(metadata or {})
    record_sync = dict(updated.get("record_sync") or {})
    object_entry = dict(record_sync.get(object_key) or {})
    existing_incremental = object_entry.get("incremental")
    existing_incremental = existing_incremental if isinstance(existing_incremental, dict) else {}

    source_cursor = cursor_source_modified_at or _parse_datetime(
        existing_incremental.get("cursor_source_modified_at")
    )

    object_entry["incremental"] = {
        "cursor_source_modified_at": _format_datetime(source_cursor)
        if source_cursor is not None
        else None,
        "cursor_updated_at": _format_datetime(cursor_updated_at),
        "lookback_minutes": _positive_int(
            lookback_minutes,
            default=DEFAULT_HUBSPOT_INCREMENTAL_LOOKBACK_MINUTES,
        ),
        "last_run_type": run_type,
        "last_seen_count": max(0, seen_count),
        "last_written_count": max(0, written_count),
        "last_skipped_stale_count": max(0, skipped_stale_count),
    }
    record_sync[object_key] = object_entry
    updated["record_sync"] = record_sync
    return updated


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        return _as_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _format_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


__all__ = [
    "DEFAULT_HUBSPOT_INCREMENTAL_LOOKBACK_MINUTES",
    "HUBSPOT_INCREMENTAL_RUN_TYPE",
    "get_hubspot_incremental_search_start",
    "is_hubspot_incremental_ready",
    "set_hubspot_incremental_cursor",
]
