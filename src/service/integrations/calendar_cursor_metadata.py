"""Calendar cursor storage and page-outcome policy shared by both workers.

Each calendar integration row is provider-specific, so one cursor key is enough:
- Outlook stores Graph `deltaLink`
- Google stores Calendar `syncToken`

Compatibility:
- Older Outlook rows may still use `outlook_delta_link`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.domain.models.organisation_tables.sync import (
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)

CALENDAR_DELTA_LINK_KEY = "calendar_delta_link"
OUTLOOK_LEGACY_DELTA_KEY = "outlook_delta_link"
CALENDAR_PARTIAL_RETRY_KEY = "calendar_partial_retry"
CALENDAR_LAST_QUARANTINE_KEY = "calendar_last_quarantine"

# A bad provider record gets two clean replays before the third identical page
# is quarantined. This bounds mailbox blockage without silently skipping on the
# first provider inconsistency.
CALENDAR_PARTIAL_RETRY_LIMIT = 3
CALENDAR_FAILED_ID_LIMIT = 20


@dataclass(frozen=True)
class CalendarSyncPersistenceOutcome:
    """Result of applying one persisted calendar page to cursor and health state."""

    cursor_advanced: bool
    quarantined: bool
    partial_attempts: int


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


def _partial_fingerprint(input_cursor: str | None, failed_record_ids: list[str]) -> str:
    payload = json.dumps(
        [input_cursor or "__initial__", sorted(failed_record_ids)],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def apply_calendar_sync_outcome(
    *,
    integration: KnowledgeIntegration,
    state: IntegrationSyncState | None,
    integration_type: IntegrationType,
    sync_result: dict[str, Any],
    input_cursor: str | None,
    next_cursor: str | None,
) -> CalendarSyncPersistenceOutcome:
    """Apply the shared cursor, quarantine, and capability-health contract.

    Provider-parent rows keep health and retry diagnostics on their calendar
    capability. Legacy direct calendar rows keep both in connection metadata,
    preserving their existing storage contract during rollout.
    """
    status = str(sync_result.get("status") or "failed")
    failed_record_ids = [
        str(value)[:200] for value in sync_result.get("failed_record_ids", []) if value is not None
    ][:CALENDAR_FAILED_ID_LIMIT]
    health_metadata = dict(
        state.sync_metadata if state is not None else integration.connection_metadata or {}
    )
    health_metadata.pop(CALENDAR_PARTIAL_RETRY_KEY, None)

    quarantined = False
    partial_attempts = 0
    cursor_advanced = status == "success" and next_cursor is not None

    if status == "partial":
        fingerprint = _partial_fingerprint(input_cursor, failed_record_ids)
        previous = (
            state.sync_metadata if state is not None else integration.connection_metadata or {}
        ).get(CALENDAR_PARTIAL_RETRY_KEY, {})
        previous_attempts = (
            int(previous.get("attempts", 0))
            if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint
            else 0
        )
        partial_attempts = previous_attempts + 1
        quarantined = partial_attempts >= CALENDAR_PARTIAL_RETRY_LIMIT and next_cursor is not None
        cursor_advanced = quarantined

        diagnostic = {
            "fingerprint": fingerprint,
            "attempts": partial_attempts,
            "failed_record_ids": failed_record_ids,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if quarantined:
            health_metadata[CALENDAR_LAST_QUARANTINE_KEY] = diagnostic
        else:
            health_metadata[CALENDAR_PARTIAL_RETRY_KEY] = diagnostic

    if state is not None:
        cursor_metadata = dict(state.cursor_metadata or {})
        if cursor_advanced:
            cursor_metadata = set_calendar_delta_link(
                cursor_metadata,
                integration_type,
                next_cursor,
            )
        state.cursor_metadata = cursor_metadata
        state.sync_metadata = health_metadata
        state.last_sync_status = status
        state.last_sync_error = (
            f"{sync_result.get('records_failed', 0)} calendar record(s) failed"
            if status == "partial"
            else None
        )
        state.consecutive_sync_failures = (
            (state.consecutive_sync_failures or 0) + 1 if status == "partial" else 0
        )
        if status == "success" or quarantined:
            state.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
    else:
        if cursor_advanced:
            health_metadata = set_calendar_delta_link(
                health_metadata,
                integration_type,
                next_cursor,
            )
        integration.connection_metadata = health_metadata
        integration.last_sync_status = status
        integration.last_sync_error = (
            f"{sync_result.get('records_failed', 0)} calendar record(s) failed"
            if status == "partial"
            else None
        )
        integration.consecutive_sync_failures = (
            (integration.consecutive_sync_failures or 0) + 1 if status == "partial" else 0
        )
        if status == "success" or quarantined:
            integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)

    return CalendarSyncPersistenceOutcome(
        cursor_advanced=cursor_advanced,
        quarantined=quarantined,
        partial_attempts=partial_attempts,
    )
