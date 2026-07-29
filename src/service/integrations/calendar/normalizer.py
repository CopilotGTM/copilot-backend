"""Provider-specific calendar payload normalization before DB writes.

This module is the *only* place in the calendar sync pipeline that knows what
Google or Microsoft event payloads look like. Everything downstream
(change_detector, writer, sync_service) consumes provider-neutral DTOs from
`types.py`, so adding a new provider is a matter of teaching this file how to
read it, not threading new keys through the rest of the pipeline.

A few provider quirks shape the design and are commented inline where they
matter:

  - Google sends recurring-exception cancellations as standalone events that
    carry `recurringEventId` + `originalStartTime` but no `iCalUID`. We flag
    those (`is_google_cancelled_exception`) so the orchestrator can route them
    differently from real shared-UID cancellations.
  - Microsoft uses an `isCancelled` boolean instead of a `status` string; we
    translate it to the Google-style `confirmed` / `cancelled` so change
    detection can compare the two providers with the same column.
  - Both providers can return malformed attendees (missing email, nested
    emailAddress wrappers, mixed casing). We tolerate those individually rather
    than failing the whole event — losing one attendee row beats losing the
    whole calendar item.
  - `iCalUID` (Google) and `iCalUId` (Microsoft) are spelled differently; both
    are the shared global UID and both map to `external_id` here.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.infra.utils.logger import get_logger
from src.service.calendar_invites.timezones import parse_outlook_datetime
from src.service.integrations.calendar.types import (
    CalendarEventIdentity,
    NormalizedCalendarAttendee,
    NormalizedCalendarEvent,
)

logger = get_logger(__name__)

# Python's datetime.fromisoformat doesn't accept the trailing "Z" suffix that
# both Google and Microsoft use for UTC. We replace it with the explicit offset
# in every parser.
ISO_UTC_SUFFIX = "+00:00"

# Provider events without a subject/summary still need *something* the timeline
# can render. Match what the existing UI displays for empty-title events.
DEFAULT_EVENT_TITLE = "(No title)"

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

EXACT_MEETING_HOSTS = {
    "meet.google.com",
    "teams.microsoft.com",
    "teams.live.com",
}

EXACT_MEETING_PROVIDERS = {
    "meet.google.com": "googleMeet",
    "teams.microsoft.com": "teamsForBusiness",
    "teams.live.com": "teamsForBusiness",
}

SUFFIX_MEETING_HOSTS = {
    "zoom.us",
    "webex.com",
}

MICROSOFT_SAFELINK_HOST_SUFFIX = ".safelinks.protection.outlook.com"


class CalendarEventNormalizer:
    """Turns Google and Microsoft event payloads into provider-neutral DTOs.

    Sync orchestration and persistence should read from NormalizedCalendarEvent
    so provider field names do not leak into write batching or change detection.
    Most methods here are stateless classmethods/staticmethods; the class exists
    primarily as a namespace and to make the boundary easy to mock in tests.
    """

    def normalize(self, event_data: dict[str, Any], provider: str) -> NormalizedCalendarEvent:
        """Convert one raw provider payload into a NormalizedCalendarEvent.

        This is the main entry point used by sync_service for every event the
        provider returns. Precomputes `is_cancelled` and the Google-exception
        flag because the incremental sync routes those payloads through
        different code paths and we don't want each branch re-parsing the raw
        dict.
        """
        external_id, occurrence_anchor, original_start_at = self.extract_identity(
            event_data,
            provider,
        )
        fields = self.extract_event_fields(event_data, provider)
        is_cancelled = fields.get("status") == "cancelled"
        return NormalizedCalendarEvent(
            provider=provider,
            provider_event_id=event_data.get("id"),
            identity=CalendarEventIdentity(
                external_id=external_id,
                occurrence_anchor=occurrence_anchor,
                original_start_at=original_start_at,
            ),
            user_external_id=self.extract_user_external_id(event_data, provider),
            series_external_id=self.extract_series_external_id(event_data, provider),
            fields=fields,
            attendees=self.extract_attendees(event_data),
            has_attendee_snapshot=self.has_attendee_snapshot(event_data),
            data_payload=event_data,
            debug_sample=self.build_debug_sample(
                event_data,
                provider=provider,
                external_id=external_id,
                occurrence_anchor=occurrence_anchor,
            ),
            is_cancelled=is_cancelled,
            is_google_cancelled_exception=self.is_google_cancelled_exception(
                event_data,
                provider,
            ),
        )

    def normalize_many(
        self,
        events: Iterable[dict[str, Any]],
        provider: str,
    ) -> list[NormalizedCalendarEvent]:
        """Normalize a provider batch for the calendar sync orchestrator."""
        return [self.normalize(event_data, provider) for event_data in events]

    @staticmethod
    def build_debug_sample(
        event_data: dict[str, Any],
        *,
        provider: str,
        external_id: str | None,
        occurrence_anchor: str,
    ) -> dict[str, Any]:
        """Build a small projection of the raw payload for log/Sentry attachment.

        We deliberately keep both `iCalUID` (Google) and `iCalUId` (Microsoft)
        because the bug we usually debug is "why did this event fail to get an
        external_id", and the answer is almost always one of these two keys
        being absent or in the wrong case. Including both makes the cause obvious
        from the log line alone.
        """
        return {
            "provider": provider,
            "provider_event_id": event_data.get("id"),
            "external_id": external_id,
            "occurrence_anchor": occurrence_anchor,
            "status": event_data.get("status") or event_data.get("isCancelled"),
            "title": event_data.get("summary") or event_data.get("subject"),
            "type": event_data.get("type"),
            "iCalUID": event_data.get("iCalUID"),
            "iCalUId": event_data.get("iCalUId"),
            "recurringEventId": event_data.get("recurringEventId"),
            "seriesMasterId": event_data.get("seriesMasterId"),
            "originalStartTime": event_data.get("originalStartTime"),
            "originalStart": event_data.get("originalStart"),
            "start": event_data.get("start"),
            "end": event_data.get("end"),
        }

    @staticmethod
    def extract_external_id(event_data: dict[str, Any], provider: str) -> str | None:
        """Extract the shared global UID stored on CalendarEvent.external_id.

        This UID is what lets two attendees on the same event resolve to one row
        in our DB even though each receives their own provider event ID. Google
        spells it `iCalUID`; Microsoft spells it `iCalUId` (lowercase d). Both
        are returned as-is — we never re-derive or hash them.
        """
        if provider == "google":
            value = event_data.get("iCalUID")
            return value if isinstance(value, str) and value else None
        if provider in ("microsoft", "outlook"):
            value = event_data.get("iCalUId")
            return value if isinstance(value, str) and value else None
        return None

    @staticmethod
    def extract_user_external_id(event_data: dict[str, Any], provider: str) -> str | None:
        """Extract per-user external ID for CalendarEventUserState.external_id.

        Unlike `extract_external_id`, this is the per-mailbox (or per-calendar)
        ID — the one each integration sees for its own copy of the event. We
        store it on user_states so a deletion delivered against this ID can be
        attributed to the right integration without false-positive cancelling
        the shared event for everyone.
        """
        if provider == "google":
            return event_data.get("id") or event_data.get("eventId")
        if provider in ("microsoft", "outlook"):
            return event_data.get("id")
        return event_data.get("id")

    @staticmethod
    def extract_series_external_id(event_data: dict[str, Any], provider: str) -> str | None:
        """Extract the provider series/master id for this mailbox copy.

        The value belongs on CalendarEventUserState, not CalendarEvent: it is
        the provider-copy series identity used to reconcile whole-series
        tombstones back to the current integration's stored occurrence rows.
        """
        if provider == "google":
            value = event_data.get("recurringEventId")
            return value if isinstance(value, str) and value else None
        if provider in ("microsoft", "outlook"):
            value = event_data.get("seriesMasterId")
            return value if isinstance(value, str) and value else None
        return None

    @classmethod
    def extract_occurrence_anchor(
        cls,
        event_data: dict[str, Any],
        provider: str,
    ) -> tuple[str, datetime | None]:
        """Compute the anchor string + parsed datetime for a recurring exception.

        For non-recurring events and for non-Google providers, the anchor is the
        empty string — the (external_id, "") pair is the canonical key. For
        Google recurring exceptions we use `originalStartTime` (the time the
        exception was *originally* scheduled for) so an exception keeps a stable
        identity even if the user moves it. Microsoft is handled differently
        because Graph already sends one row per exception with its own iCalUId.
        """
        if provider == "google":
            original_start = event_data.get("originalStartTime")
            if isinstance(original_start, dict):
                if isinstance(original_start.get("dateTime"), str):
                    parsed = cls.parse_google_datetime(
                        original_start["dateTime"],
                        original_start.get("timeZone"),
                    )
                    return cls.format_occurrence_anchor(parsed), parsed
                if isinstance(original_start.get("date"), str):
                    parsed = cls.parse_iso_date(original_start["date"])
                    return cls.format_occurrence_anchor(parsed), parsed
            return "", None

        if provider in ("microsoft", "outlook"):
            return "", None

        return "", None

    @staticmethod
    def format_occurrence_anchor(value: datetime | None) -> str:
        """Format a datetime as the canonical anchor string (UTC, "Z" suffix).

        The anchor is part of the unique index on calendar_events, so the format
        must be byte-stable across syncs. We always coerce to UTC and use the
        "Z" suffix instead of "+00:00" so two sync runs of the same exception
        produce the same anchor string regardless of the originating timezone.
        """
        if value is None:
            return ""
        normalized = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.isoformat().replace("+00:00", "Z")

    @classmethod
    def extract_identity(
        cls,
        event_data: dict[str, Any],
        provider: str,
    ) -> tuple[str | None, str, datetime | None]:
        """Return the (external_id, occurrence_anchor, original_start_at) triple.

        Convenience wrapper that combines `extract_external_id` and
        `extract_occurrence_anchor` so callers (and the orchestrator's
        prefetch path) can grab the lookup key in one call.
        """
        external_id = cls.extract_external_id(event_data, provider)
        occurrence_anchor, original_start_at = cls.extract_occurrence_anchor(event_data, provider)
        return external_id, occurrence_anchor, original_start_at

    @staticmethod
    def is_google_cancelled_exception(event_data: dict[str, Any], provider: str) -> bool:
        """Return True for the Google recurring-exception-cancellation quirk.

        When a user removes a single occurrence of a recurring meeting in Google
        Calendar, Google delivers it as a `status=cancelled` event that has a
        `recurringEventId` and `originalStartTime` but no `iCalUID`. The
        orchestrator needs to handle these differently from real shared-UID
        cancellations because the lookup keys are not the same.
        """
        if provider != "google":
            return False
        if event_data.get("status") != "cancelled":
            return False
        return bool(event_data.get("recurringEventId") and event_data.get("originalStartTime"))

    @classmethod
    def extract_event_fields(cls, event_data: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract provider-neutral CalendarEvent column values from one raw payload."""
        if provider == "google":
            return cls._extract_google_event_fields(event_data)
        if provider in ("microsoft", "outlook"):
            return cls._extract_microsoft_event_fields(event_data)
        logger.warning("Unknown provider %s, using generic extraction", provider)
        return cls._extract_generic_event_fields(event_data)

    @classmethod
    def _extract_google_event_fields(cls, event_data: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "title": event_data.get("summary") or DEFAULT_EVENT_TITLE,
            "description": event_data.get("description"),
            "location": event_data.get("location"),
            "status": event_data.get("status"),
            "calendar_external_id": event_data.get("calendarId"),
        }
        cls._apply_google_time_fields(fields, event_data)
        cls._apply_google_conference(fields, event_data.get("conferenceData"))
        cls._apply_join_link_from_candidates(
            fields,
            cls._google_join_link_candidates(event_data),
        )
        cls._apply_google_organizer(fields, event_data.get("organizer"))
        cls._apply_attendee_count(fields, event_data.get("attendees"))
        cls._apply_google_source_modified(fields, event_data)
        return fields

    @classmethod
    def extract_join_link_update(
        cls,
        provider: str | None,
        event_data: dict[str, Any],
    ) -> dict[str, str] | None:
        """Return only the join-link fields that a sparse backfill should write."""
        provider_name = (provider or "").lower()
        if provider_name in {"microsoft", "outlook"}:
            candidates = cls._microsoft_join_link_candidates(event_data)
        elif provider_name == "google":
            candidates = cls._google_join_link_candidates(event_data)
        else:
            candidates = cls._unknown_provider_join_link_candidates(event_data)

        fields: dict[str, Any] = {}
        cls._apply_join_link_from_candidates(fields, candidates, replace_existing=True)
        conference_link = fields.get("conference_link")
        online_meeting_provider = fields.get("online_meeting_provider")
        if not isinstance(conference_link, str) or not isinstance(online_meeting_provider, str):
            return None
        return {
            "conference_link": conference_link,
            "online_meeting_provider": online_meeting_provider,
        }

    @classmethod
    def _apply_join_link_from_candidates(
        cls,
        fields: dict[str, Any],
        candidates: Iterable[Any],
        *,
        replace_existing: bool = False,
    ) -> None:
        if fields.get("conference_link") and not replace_existing:
            return
        for candidate in candidates:
            meeting_url = cls._extract_supported_meeting_url(candidate)
            if meeting_url is None:
                continue
            provider = cls._meeting_provider_from_url(meeting_url)
            if provider is None:
                continue
            fields["conference_link"] = meeting_url
            fields["online_meeting_provider"] = provider
            return

    @classmethod
    def _extract_supported_meeting_url(cls, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        for raw_url in cls._iter_urls(value):
            cleaned = cls._clean_candidate_url(raw_url)
            candidate = cls._unwrap_microsoft_safelink(cleaned) or cleaned
            if cls._meeting_provider_from_url(candidate):
                return candidate
        return None

    @staticmethod
    def _iter_urls(value: str) -> Iterable[str]:
        for match in URL_PATTERN.finditer(value):
            yield match.group(0)

    @staticmethod
    def _clean_candidate_url(value: str) -> str:
        return html.unescape(value.strip().rstrip(").,;]"))

    @staticmethod
    def _unwrap_microsoft_safelink(value: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(value)
            host = (parsed.hostname or "").lower()
            if not host.endswith(MICROSOFT_SAFELINK_HOST_SUFFIX):
                return None
            encoded_urls = urllib.parse.parse_qs(parsed.query).get("url", [])
            if not encoded_urls:
                return None
            return encoded_urls[0]
        except Exception:
            return None

    @staticmethod
    def _meeting_provider_from_url(value: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(value)
        except Exception:
            return None

        if parsed.scheme not in {"http", "https"}:
            return None

        host = (parsed.hostname or "").lower()
        if host in EXACT_MEETING_HOSTS:
            return EXACT_MEETING_PROVIDERS[host]
        for suffix_host in SUFFIX_MEETING_HOSTS:
            if host == suffix_host or host.endswith(f".{suffix_host}"):
                return "zoom" if suffix_host == "zoom.us" else "webex"
        return None

    @classmethod
    def _apply_google_time_fields(cls, fields: dict[str, Any], event_data: dict[str, Any]) -> None:
        start = event_data.get("start")
        if isinstance(start, dict):
            cls._set_google_start(fields, start)
            fields["timezone"] = start.get("timeZone")

        end = event_data.get("end")
        if isinstance(end, dict):
            cls._set_google_end(fields, end)

    @classmethod
    def _set_google_start(cls, fields: dict[str, Any], start: dict[str, Any]) -> None:
        # Google distinguishes timed events (`dateTime`) from all-day events
        # (`date`). Picking the right key also tells us `is_all_day`, which
        # change detection compares as a separate column.
        if "dateTime" in start:
            fields["start_at"] = cls.parse_google_datetime(
                start["dateTime"],
                start.get("timeZone"),
            )
            fields["is_all_day"] = False
        elif "date" in start:
            fields["start_at"] = cls.parse_iso_date(start["date"])
            fields["is_all_day"] = True

    @classmethod
    def _set_google_end(cls, fields: dict[str, Any], end: dict[str, Any]) -> None:
        if "dateTime" in end:
            fields["end_at"] = cls.parse_google_datetime(
                end["dateTime"],
                end.get("timeZone"),
            )
        elif "date" in end:
            fields["end_at"] = cls.parse_iso_date(end["date"])

    @classmethod
    def _apply_google_conference(cls, fields: dict[str, Any], conference_data: Any) -> None:
        if not isinstance(conference_data, dict):
            return

        video_uri = None
        for entry in conference_data.get("entryPoints", []):
            if entry.get("entryPointType") == "video":
                video_uri = entry.get("uri")
                break

        solution = conference_data.get("conferenceSolution", {})
        solution_type = solution.get("key", {}).get("type", "")

        if solution_type == "hangoutsMeet":
            if not isinstance(video_uri, str):
                return
            meeting_url = cls._extract_supported_meeting_url(video_uri)
            if meeting_url is None:
                return
            fields["conference_link"] = meeting_url
            fields["online_meeting_provider"] = "googleMeet"
            fields["online_meeting_id"] = conference_data.get("conferenceId")
            return

        if isinstance(video_uri, str):
            meeting_url = cls._extract_supported_meeting_url(video_uri)
            if meeting_url is None:
                return
            provider = cls._meeting_provider_from_url(meeting_url)
            if provider is None:
                return
            fields["conference_link"] = meeting_url
            fields["online_meeting_provider"] = provider
            fields["online_meeting_id"] = conference_data.get("conferenceId")

    @staticmethod
    def _apply_google_organizer(fields: dict[str, Any], organizer: Any) -> None:
        if isinstance(organizer, dict):
            fields["organiser_email"] = organizer.get("email")
            fields["organiser_name"] = organizer.get("displayName")

    @classmethod
    def _apply_google_source_modified(
        cls,
        fields: dict[str, Any],
        event_data: dict[str, Any],
    ) -> None:
        updated = event_data.get("updated")
        if updated:
            fields["source_modified_at"] = cls.parse_iso_datetime(updated)

    @classmethod
    def _extract_microsoft_event_fields(cls, event_data: dict[str, Any]) -> dict[str, Any]:
        # Microsoft uses an `isCancelled` boolean instead of Google's `status`
        # string. Translate to Google's vocabulary so change detection and the
        # cancellation-routing logic in sync_service can use one column.
        is_cancelled = event_data.get("isCancelled", False)
        status = "cancelled" if is_cancelled else "confirmed"
        fields = {
            "title": event_data.get("subject") or DEFAULT_EVENT_TITLE,
            # Prefer `bodyPreview` (already plain text) over the full HTML body.
            # Falling back to `body.content` keeps coverage on payloads that
            # omit the preview.
            "description": event_data.get("bodyPreview")
            or event_data.get("body", {}).get("content"),
            "location": event_data.get("location", {}).get("displayName"),
            "status": status,
        }
        cls._apply_microsoft_times(fields, event_data)
        cls._apply_microsoft_organizer(fields, event_data)
        cls._apply_microsoft_online_meeting(fields, event_data)
        cls._apply_attendee_count(fields, event_data.get("attendees"))
        cls._apply_microsoft_source_modified(fields, event_data)
        return fields

    @classmethod
    def _apply_microsoft_times(cls, fields: dict[str, Any], event_data: dict[str, Any]) -> None:
        start = event_data.get("start")
        if isinstance(start, dict):
            date_time = start.get("dateTime")
            if date_time:
                # Graph's DateTimeTimeZone is a pair: offset-less dateTime is a
                # wall clock in timeZone, not UTC. Parse the pair at this provider
                # boundary so every downstream writer receives a real instant.
                fields["start_at"] = parse_outlook_datetime(date_time, start.get("timeZone"))
            fields["timezone"] = start.get("timeZone")
            # Microsoft puts the all-day flag on the event, not on the start
            # block (Google puts it on `start` by virtue of using `date` vs
            # `dateTime`). Mirror that into the same column for both providers.
            fields["is_all_day"] = event_data.get("isAllDay", False)

        end = event_data.get("end")
        if isinstance(end, dict):
            date_time = end.get("dateTime")
            if date_time:
                fields["end_at"] = parse_outlook_datetime(date_time, end.get("timeZone"))

    @staticmethod
    def _apply_microsoft_organizer(fields: dict[str, Any], event_data: dict[str, Any]) -> None:
        organizer = event_data.get("organizer", {}).get("emailAddress", {})
        if organizer:
            fields["organiser_email"] = organizer.get("address")
            fields["organiser_name"] = organizer.get("name")

    @staticmethod
    def _microsoft_join_link_candidates(event_data: dict[str, Any]) -> Iterable[Any]:
        online_meeting = event_data.get("onlineMeeting")
        if isinstance(online_meeting, dict):
            yield online_meeting.get("joinUrl")

        yield event_data.get("onlineMeetingUrl")

        location = event_data.get("location")
        if isinstance(location, dict):
            yield location.get("displayName")
            yield location.get("locationUri")
        else:
            yield location

        locations = event_data.get("locations")
        if isinstance(locations, list):
            for item in locations:
                if not isinstance(item, dict):
                    continue
                yield item.get("displayName")
                yield item.get("locationUri")
                yield item.get("uniqueId")

        body = event_data.get("body")
        if isinstance(body, dict):
            yield body.get("content")
        yield event_data.get("bodyPreview")

    @staticmethod
    def _google_join_link_candidates(event_data: dict[str, Any]) -> Iterable[Any]:
        conference_data = event_data.get("conferenceData")
        if isinstance(conference_data, dict):
            entry_points = conference_data.get("entryPoints")
            if isinstance(entry_points, list):
                for entry in entry_points:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("entryPointType") == "video":
                        yield entry.get("uri")
        yield event_data.get("hangoutLink")
        yield event_data.get("location")
        yield event_data.get("description")

    @staticmethod
    def _generic_join_link_candidates(event_data: dict[str, Any]) -> Iterable[Any]:
        yield event_data.get("conference_link")
        yield event_data.get("location")
        yield event_data.get("description")
        yield event_data.get("bodyPreview")

    @classmethod
    def _unknown_provider_join_link_candidates(cls, event_data: dict[str, Any]) -> Iterable[Any]:
        yield from cls._google_join_link_candidates(event_data)
        yield from cls._microsoft_join_link_candidates(event_data)
        yield from cls._generic_join_link_candidates(event_data)

    @classmethod
    def _apply_microsoft_online_meeting(
        cls,
        fields: dict[str, Any],
        event_data: dict[str, Any],
    ) -> None:
        cls._apply_join_link_from_candidates(
            fields,
            cls._microsoft_join_link_candidates(event_data),
        )

        join_url = fields.get("conference_link")
        if not isinstance(join_url, str):
            return

        if fields.get("online_meeting_provider") == "teamsForBusiness":
            fields["online_meeting_provider"] = "teamsForBusiness"
            meeting_id = cls.extract_teams_meeting_id(join_url)
            if meeting_id:
                fields["online_meeting_id"] = meeting_id

    @staticmethod
    def extract_teams_meeting_id(join_url: str) -> str | None:
        """Pull the Teams meeting ID out of a Teams join URL.

        The ID is the URL-encoded path segment shaped like
        `19:meeting_<id>@thread.v2`. We URL-decode each segment because Graph
        sometimes percent-encodes the `:` and the `@`. A failure here is logged
        and swallowed — a missing meeting ID is recoverable (we still have the
        join URL itself) so we don't want to fail the whole event sync over it.
        """
        try:
            parsed = urllib.parse.urlparse(join_url)
            for part in parsed.path.split("/"):
                if not part:
                    continue
                decoded = urllib.parse.unquote(part)
                if decoded.startswith("19:meeting_") and "@thread" in decoded:
                    return decoded
        except Exception as e:
            logger.warning("Failed to extract Teams meeting ID from URL: %s", str(e))
        return None

    @classmethod
    def _apply_microsoft_source_modified(
        cls,
        fields: dict[str, Any],
        event_data: dict[str, Any],
    ) -> None:
        modified = event_data.get("lastModifiedDateTime")
        if modified:
            fields["source_modified_at"] = cls.parse_iso_datetime(modified)

    @staticmethod
    def _extract_generic_event_fields(event_data: dict[str, Any]) -> dict[str, Any]:
        location = event_data.get("location")
        if isinstance(location, dict):
            location = location.get("displayName")

        return {
            "title": event_data.get("summary") or event_data.get("subject") or DEFAULT_EVENT_TITLE,
            "description": event_data.get("description"),
            "location": location,
            "status": event_data.get("status"),
        }

    @staticmethod
    def _apply_attendee_count(fields: dict[str, Any], attendees: Any) -> None:
        fields["attendee_count"] = len(attendees) if isinstance(attendees, list) else 0

    @classmethod
    def extract_attendees(cls, event_data: dict[str, Any]) -> list[NormalizedCalendarAttendee]:
        """Build the per-attendee snapshot, dropping rows we cannot identify.

        We tolerate malformed entries (no email, mixed casing, nested wrappers)
        rather than failing the whole event — losing one attendee is acceptable;
        losing the entire event is not. The organizer flag is computed both from
        the provider's `organizer` boolean and by cross-referencing the event's
        organizer email, because Google sets the boolean reliably while
        Microsoft puts the organizer in a separate top-level payload.
        """
        attendees_data = event_data.get("attendees", [])
        if not isinstance(attendees_data, list):
            return []

        organizer_email = cls.extract_organizer_email(event_data)
        attendees: list[NormalizedCalendarAttendee] = []
        for attendee_data in attendees_data:
            if not isinstance(attendee_data, dict):
                continue
            email = cls.extract_attendee_email(attendee_data)
            if not email:
                # Skip attendees without an email — without one we have no key
                # to dedupe against and downstream code (timeline, agents) keys
                # roster membership by email.
                continue
            is_organiser = attendee_data.get("organizer", False) or (
                organizer_email and email.lower() == organizer_email.lower()
            )
            attendees.append(
                NormalizedCalendarAttendee(
                    email=email,
                    display_name=cls.extract_attendee_name(attendee_data),
                    response_status=cls.normalize_response_status(
                        cls.extract_attendee_response(attendee_data)
                    ),
                    is_organiser=bool(is_organiser),
                    data_payload=attendee_data,
                )
            )
        return attendees

    @staticmethod
    def has_attendee_snapshot(event_data: dict[str, Any]) -> bool:
        """Return True when the provider sent any attendee data at all.

        Snapshot presence is based on the *provider payload*, not on whether the
        attendees parse cleanly. A changed event with only malformed attendees
        should still clear the old roster — the provider is telling us the
        roster has been edited, even if every entry it sent is unusable.
        """
        attendees_data = event_data.get("attendees", [])
        return isinstance(attendees_data, list) and bool(attendees_data)

    @staticmethod
    def extract_attendee_email(attendee_data: dict[str, Any]) -> str | None:
        """Pull an attendee email from either the Google or Microsoft shape.

        Google uses a flat `email` field; Microsoft nests it under
        `emailAddress.address`. Both are checked so the same call works for
        either provider's attendee payload.
        """
        email = attendee_data.get("email")
        if isinstance(email, str) and email:
            return email

        email_obj = attendee_data.get("emailAddress")
        if isinstance(email_obj, dict):
            address = email_obj.get("address")
            return address if isinstance(address, str) and address else None

        return None

    @staticmethod
    def extract_attendee_name(attendee_data: dict[str, Any]) -> str | None:
        """Pull a display name from either Google attendee data or Graph emailAddress data."""
        display_name = attendee_data.get("displayName")
        if isinstance(display_name, str) and display_name:
            return display_name

        email_obj = attendee_data.get("emailAddress")
        if isinstance(email_obj, dict):
            name = email_obj.get("name")
            return name if isinstance(name, str) and name else None

        return None

    @staticmethod
    def extract_attendee_response(attendee_data: dict[str, Any]) -> str:
        """Pull the RSVP response string from either provider's attendee shape.

        Google uses `responseStatus` directly; Microsoft nests it under
        `status.response`. We default to `"none"` rather than None so the
        downstream `normalize_response_status` mapping always has a string to
        work with.
        """
        response_status = attendee_data.get("responseStatus")
        if isinstance(response_status, str):
            return response_status

        status_obj = attendee_data.get("status")
        if isinstance(status_obj, dict):
            response = status_obj.get("response")
            return response if isinstance(response, str) and response else "none"

        return "none"

    @staticmethod
    def extract_organizer_email(event_data: dict[str, Any]) -> str | None:
        """Return the organizer email from either provider's top-level organizer block."""
        organizer = event_data.get("organizer")
        if not isinstance(organizer, dict):
            return None

        email = organizer.get("email")
        if isinstance(email, str) and email:
            return email

        email_obj = organizer.get("emailAddress")
        if isinstance(email_obj, dict):
            address = email_obj.get("address")
            return address if isinstance(address, str) and address else None

        return None

    @staticmethod
    def parse_iso_datetime(value: str) -> datetime:
        """Parse provider ISO datetimes, accepting the UTC `Z` suffix both APIs emit."""
        parsed = datetime.fromisoformat(value.replace("Z", ISO_UTC_SUFFIX))
        if parsed.tzinfo is None:
            raise ValueError("Provider datetime is missing an offset")
        return parsed.astimezone(UTC)

    @staticmethod
    def parse_google_datetime(value: str, timezone_name: str | None) -> datetime:
        """Resolve Google's RFC3339-or-timeZone pair into one UTC instant."""
        parsed = datetime.fromisoformat(value.replace("Z", ISO_UTC_SUFFIX))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
        if not timezone_name:
            raise ValueError("Google dateTime is missing both offset and timeZone")
        return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)

    @staticmethod
    def parse_iso_date(value: str) -> datetime:
        """Parse all-day provider dates as midnight UTC datetimes for DB storage."""
        return datetime.fromisoformat(f"{value}T00:00:00{ISO_UTC_SUFFIX}")

    @staticmethod
    def normalize_response_status(status: str) -> str:
        """Map provider RSVP strings onto the canonical Google-style vocabulary.

        Google uses `needsAction`; Microsoft sometimes sends `needs_action`
        (snake_case). Both must collapse to the same value so attendee snapshots
        across providers are diff-comparable. Unknown strings collapse to
        `"none"` rather than raising — we'd rather store an unknown response
        as no-response than fail the whole attendee row.
        """
        status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentative": "tentative",
            "needsAction": "needsAction",
            "needs_action": "needsAction",
            "none": "none",
        }
        return status_map.get(status.lower(), "none")
