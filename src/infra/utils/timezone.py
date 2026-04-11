"""Shared timezone and datetime-anchor utilities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USER_TIMEZONE_HEADER = "X-User-Timezone"
DEFAULT_TIMEZONE = "Etc/UTC"


def validate_timezone_name(value: str | None) -> str | None:
    """Return a normalized IANA timezone or None if absent/invalid."""

    if value is None:
        return None

    timezone_name = value.strip()
    if not timezone_name:
        return None

    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None

    return timezone_name


def resolve_timezone_name(*candidates: str | None, default: str | None = DEFAULT_TIMEZONE) -> str | None:
    """Resolve the first valid IANA timezone from the provided candidates."""

    for candidate in candidates:
        timezone_name = validate_timezone_name(candidate)
        if timezone_name:
            return timezone_name
    return default


def parse_datetime_anchor(value: str | datetime | None) -> datetime | None:
    """Parse a stored datetime anchor into a timezone-aware UTC datetime."""

    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed.astimezone(UTC)


def get_context_anchor_utc(metadata: dict[str, Any] | None) -> datetime:
    """Return a UTC anchor for relative-date calculations."""

    if metadata:
        anchored = parse_datetime_anchor(metadata.get("current_datetime"))
        if anchored:
            return anchored
    return datetime.now(UTC)


def get_context_timezone_name(metadata: dict[str, Any] | None) -> str:
    """Return the effective timezone for a context."""

    if not metadata:
        return DEFAULT_TIMEZONE
    return resolve_timezone_name(metadata.get("user_timezone")) or DEFAULT_TIMEZONE


def get_context_anchor_local(metadata: dict[str, Any] | None) -> datetime:
    """Return the anchor converted into the effective local timezone."""

    anchor_utc = get_context_anchor_utc(metadata)
    timezone_name = get_context_timezone_name(metadata)
    return anchor_utc.astimezone(ZoneInfo(timezone_name))


__all__ = [
    "DEFAULT_TIMEZONE",
    "USER_TIMEZONE_HEADER",
    "get_context_anchor_local",
    "get_context_anchor_utc",
    "get_context_timezone_name",
    "parse_datetime_anchor",
    "resolve_timezone_name",
    "validate_timezone_name",
]
