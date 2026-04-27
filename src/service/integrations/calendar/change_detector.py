"""Calendar row change detection for normalized provider payloads.

Called by CalendarSyncService.sync_calendar_events_incremental for every event
the provider returns. The function decides one thing: "does this incoming event
differ enough from the row we already have to justify rewriting calendar_events
and replacing the attendee snapshot?"

Why this matters: providers re-deliver unchanged events on every sync cursor
read. If we rewrote rows on every delivery, we'd pay an UPDATE per event plus a
delete-and-replace of the attendee roster — turning incremental sync into a
silent N+1 against the org schema. This module is what keeps the no-op path
free.

The comparison philosophy:
  - Required fields (title, status, organiser, etc.) must match exactly. A None
    on either side that the other side fills counts as a change.
  - Optional fields (timezone, calendar_external_id, etc.) only count as a
    change when the incoming value is non-None — a sparse provider payload
    should never force a rewrite just because it stopped sending an optional
    field this cycle.
  - Datetime comparisons strip tzinfo because providers and our DB columns mix
    naive/aware representations and we only care about wall-clock equality here.
"""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.service.integrations.calendar.types import NormalizedCalendarEvent


def has_meaningful_event_changes(
    existing: CalendarEvent,
    incoming: NormalizedCalendarEvent,
) -> bool:
    """Return whether a normalized provider row should rewrite calendar_events.

    Returns True when the incoming payload differs from the persisted row in any
    way that affects downstream consumers (deal timeline, briefing agent, etc.).
    Returns False when the incoming payload is functionally identical, in which
    case the orchestrator skips the row write but still refreshes user state.

    Caller (sync_service) treats a False return as "skip the event upsert and
    the attendee replacement, but keep updating the per-user CalendarEventUserState
    so deletions stay observable per integration."
    """
    fields = incoming.fields

    # Provider-stamped modification time is the cheapest signal: if the provider
    # tells us this event was modified after our last sync, trust it without
    # walking every field.
    incoming_modified = fields.get("source_modified_at")
    if (
        incoming_modified
        and existing.source_modified_at
        and incoming_modified > existing.source_modified_at
    ):
        return True

    # Required fields — a None vs non-None mismatch counts as a change. These
    # are the columns the timeline and agents actually display.
    checks: list[tuple[Any, Any]] = [
        (existing.title, fields.get("title")),
        (existing.description, fields.get("description")),
        (existing.location, fields.get("location")),
        (existing.status, fields.get("status")),
        (existing.conference_link, fields.get("conference_link")),
        (existing.organiser_email, fields.get("organiser_email")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    # Optional fields are compared only when the provider sent them this cycle.
    # Sparse provider payloads (e.g., a Graph delta that drops `timeZone`) must
    # not force a rewrite — that would re-introduce the N+1 we're guarding here.
    optional_checks: list[tuple[Any, Any]] = [
        (existing.organiser_name, fields.get("organiser_name")),
        (existing.online_meeting_provider, fields.get("online_meeting_provider")),
        (existing.online_meeting_id, fields.get("online_meeting_id")),
        (existing.calendar_external_id, fields.get("calendar_external_id")),
        (existing.timezone, fields.get("timezone")),
    ]

    for existing_val, incoming_val in optional_checks:
        if incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    # Strip tzinfo before comparing — providers send aware ISO timestamps but
    # the DB column may round-trip as naive depending on the driver. Wall-clock
    # equality is what callers care about; offset jitter would cause spurious
    # rewrites on every sync.
    incoming_start = fields.get("start_at")
    if (
        incoming_start
        and existing.start_at
        and incoming_start.replace(tzinfo=None) != existing.start_at.replace(tzinfo=None)
    ):
        return True

    incoming_end = fields.get("end_at")
    if (
        incoming_end
        and existing.end_at
        and incoming_end.replace(tzinfo=None) != existing.end_at.replace(tzinfo=None)
    ):
        return True

    # Attendee count change is the cheap proxy for "roster moved." A full
    # roster diff happens later in the writer when we replace the snapshot.
    incoming_attendee_count = fields.get("attendee_count", 0)
    if existing.attendee_count != incoming_attendee_count:
        return True

    incoming_is_all_day = bool(fields.get("is_all_day", False))
    return existing.is_all_day != incoming_is_all_day
