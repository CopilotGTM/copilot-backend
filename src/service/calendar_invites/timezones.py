"""Timezone conversion helpers for Microsoft Outlook Calendar."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tzlocal.windows_tz import win_tz

_WINDOWS_TO_IANA = dict(win_tz)
_IANA_TO_WINDOWS = {iana_name: windows_name for windows_name, iana_name in win_tz.items()}
_IANA_ALIASES = {
    "Asia/Kolkata": "Asia/Calcutta",
    "Etc/UTC": "UTC",
}


def to_outlook_timezone(timezone_name: str) -> str:
    """Map an IANA timezone name to a Microsoft Graph-supported Windows name."""
    cleaned = (timezone_name or "").strip()
    if not cleaned:
        raise ValueError("timezone is required")

    if cleaned in _WINDOWS_TO_IANA:
        # Already in Windows-name form from the UI; keep it unchanged to avoid
        # unnecessary mapping drift and preserve existing Graph behavior.
        return cleaned

    normalized = _IANA_ALIASES.get(cleaned, cleaned)
    if normalized == "UTC":
        return "UTC"

    # Validate the IANA identifier now so bad values fail at parse time and do not
    # reach event creation with a silent default.
    ZoneInfo(normalized)
    windows_name = _IANA_TO_WINDOWS.get(normalized)
    if not windows_name:
        raise ValueError(f"Unsupported calendar timezone: {timezone_name}")
    return windows_name


def parse_outlook_datetime(value: str, timezone_name: str | None) -> datetime:
    """Parse Graph's DateTimeTimeZone pair into one UTC-aware instant.

    Graph often sends a local wall-clock string without an offset and carries
    its Windows timezone separately. Treating that string as UTC shifts every
    India meeting by 5:30, so only offset-bearing values may bypass the zone.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)

    cleaned = (timezone_name or "").strip()
    if not cleaned:
        raise ValueError("Microsoft dateTime is missing timeZone")
    iana_name = _WINDOWS_TO_IANA.get(cleaned, _IANA_ALIASES.get(cleaned, cleaned))
    return parsed.replace(tzinfo=ZoneInfo(iana_name)).astimezone(UTC)
