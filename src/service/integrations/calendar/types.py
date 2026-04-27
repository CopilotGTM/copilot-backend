"""Shared DTOs for calendar provider normalization and persistence.

These dataclasses are the provider-neutral shape that flows from the normalizer
into change detection and the writer. Sync orchestration should never read raw
Google or Microsoft field names — everything funnels through these types so a
new provider can be added without touching change detection or write logic.

All four types are frozen so a single normalized event can be safely shared
between batched DB operations (event upsert, attendee snapshot, user-state
upsert) without anyone mutating it mid-flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CalendarEventIdentity:
    """Composite identity used to de-duplicate events across providers and recurrences.

    `external_id` is the shared global UID (Google `iCalUID` / Microsoft `iCalUId`)
    that lets two attendees see the same event row in our DB. `occurrence_anchor`
    distinguishes a recurring series from one of its exceptions; for non-recurring
    events it is the empty string. `original_start_at` is the parsed anchor time
    we keep around for debugging when an exception's anchor string looks wrong.
    """

    external_id: str | None
    occurrence_anchor: str
    original_start_at: datetime | None


@dataclass(frozen=True)
class NormalizedCalendarAttendee:
    """One attendee row, provider-neutral.

    `is_organiser` is precomputed because both providers carry the organiser flag
    in different places (Google: `attendee.organizer`; Microsoft: a separate
    organizer payload that we cross-reference by email). `data_payload` keeps the
    original provider blob so debugging never has to re-fetch from the provider.
    """

    email: str
    display_name: str | None
    response_status: str
    is_organiser: bool
    data_payload: dict[str, Any]


@dataclass(frozen=True)
class NormalizedCalendarEvent:
    """Provider-neutral event shape passed from parsing into change detection and writes.

    `is_cancelled` and `is_google_cancelled_exception` are precomputed because the
    incremental sync routes cancellations through different paths than normal
    upserts (see CalendarSyncService.sync_calendar_events_incremental), and we
    want that routing to read a flag rather than re-parse the provider payload.

    `has_attendee_snapshot` reflects whether the provider sent any attendee data
    at all — even malformed entries count, because a payload with attendees is
    the provider's signal that the roster has changed and should replace the
    existing snapshot.
    """

    provider: str
    provider_event_id: str | None
    identity: CalendarEventIdentity
    user_external_id: str | None
    fields: dict[str, Any]
    attendees: list[NormalizedCalendarAttendee]
    has_attendee_snapshot: bool
    data_payload: dict[str, Any]
    debug_sample: dict[str, Any]
    is_cancelled: bool
    is_google_cancelled_exception: bool


@dataclass(frozen=True)
class CalendarEventUpsertBatchItem:
    event: NormalizedCalendarEvent
