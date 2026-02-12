"""Simple helpers for calendar incremental cursor storage in integration metadata.

Each calendar integration row is provider-specific, so one cursor key is enough:
- Outlook stores Graph `deltaLink`
- Google stores Calendar `syncToken`

Compatibility:
- Older Outlook rows may still use `outlook_delta_link`.
"""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync import IntegrationType

CALENDAR_DELTA_LINK_KEY = "calendar_delta_link"
OUTLOOK_LEGACY_DELTA_KEY = "outlook_delta_link"


def _copy_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return a safe mutable copy of connection metadata."""
    return dict(metadata or {})


def get_calendar_delta_link(
    metadata: dict[str, Any] | None,
    integration_type: IntegrationType,
) -> str | None:
    """Read calendar cursor for the given integration type."""
    current = (metadata or {}).get(CALENDAR_DELTA_LINK_KEY)
    if isinstance(current, str) and current:
        return current

    if integration_type == IntegrationType.OUTLOOK_CALENDAR:
        legacy = (metadata or {}).get(OUTLOOK_LEGACY_DELTA_KEY)
        if isinstance(legacy, str) and legacy:
            return legacy
    return None


def set_calendar_delta_link(
    metadata: dict[str, Any] | None,
    integration_type: IntegrationType,
    delta_link: str | None,
) -> dict[str, Any]:
    """Persist calendar cursor for the given integration type."""
    updated = _copy_metadata(metadata)

    if delta_link:
        updated[CALENDAR_DELTA_LINK_KEY] = delta_link
        if integration_type == IntegrationType.OUTLOOK_CALENDAR:
            updated[OUTLOOK_LEGACY_DELTA_KEY] = delta_link
    else:
        updated.pop(CALENDAR_DELTA_LINK_KEY, None)
        if integration_type == IntegrationType.OUTLOOK_CALENDAR:
            updated.pop(OUTLOOK_LEGACY_DELTA_KEY, None)

    return updated
