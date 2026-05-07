"""Focused tests for calendar provider normalization and change detection.

These tests exercise the contracts that the rest of the calendar sync pipeline
silently relies on:

  - Malformed attendees are dropped individually, not failing the whole event.
  - Google and Microsoft attendee shapes both produce the same NormalizedAttendee.
  - Change detection treats a fully-equivalent re-sync as a no-op so the
    incremental path can skip the event row + attendee rewrite for unchanged
    events (the no-op skip is what removed the production N+1).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from src.service.integrations.calendar.change_detector import has_meaningful_event_changes
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer


def test_google_normalizer_skips_attendees_without_email() -> None:
    """Malformed attendees (no email) are dropped, the event itself is kept.

    Locks the contract that one bad attendee row cannot fail the whole event —
    losing one attendee is acceptable; losing the entire calendar event is not.
    """
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-ical",
            "summary": "Team Sync",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T10:30:00Z", "timeZone": "UTC"},
            "organizer": {"email": "host@example.com", "displayName": "Host"},
            "attendees": [
                {"email": "host@example.com", "responseStatus": "accepted"},
                {"displayName": "Missing Email"},
            ],
        },
        "google",
    )

    assert normalized.identity.external_id == "shared-ical"
    assert normalized.user_external_id == "google-user-event"
    assert normalized.has_attendee_snapshot is True
    assert [attendee.email for attendee in normalized.attendees] == ["host@example.com"]
    assert normalized.attendees[0].is_organiser is True


def test_microsoft_normalizer_reads_nested_attendee_payload() -> None:
    """Microsoft's `emailAddress.{address,name}` shape produces the same DTO as Google.

    Locks the contract that downstream code (change detection, writer) sees
    one provider-neutral attendee shape regardless of whether the source was
    Google or Microsoft.
    """
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Pipeline Review",
            "isCancelled": False,
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T10:30:00Z", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"address": "host@example.com", "name": "Host"}},
            "attendees": [
                {
                    "emailAddress": {"address": "buyer@example.com", "name": "Buyer"},
                    "status": {"response": "accepted"},
                }
            ],
        },
        "microsoft",
    )

    assert normalized.identity.external_id == "shared-ms-ical"
    assert normalized.user_external_id == "ms-user-event"
    assert normalized.fields["title"] == "Pipeline Review"
    assert normalized.attendees[0].email == "buyer@example.com"
    assert normalized.attendees[0].display_name == "Buyer"


def test_change_detector_uses_normalized_fields_for_state_only_skip() -> None:
    """Equivalent re-sync of an event must not flag a meaningful change.

    Locks the no-op skip path that removed the production N+1: when nothing
    has actually changed, change_detector returns False and the orchestrator
    skips both the calendar_events row write and the attendee snapshot
    replace, even though it still refreshes the per-user state row.
    """
    existing = SimpleNamespace(
        title="Team Sync",
        description=None,
        location=None,
        status="confirmed",
        conference_link=None,
        organiser_email="host@example.com",
        organiser_name="Host",
        online_meeting_provider=None,
        online_meeting_id=None,
        calendar_external_id=None,
        timezone="UTC",
        start_at=datetime(2024, 1, 15, 10, 0, 0),
        end_at=datetime(2024, 1, 15, 10, 30, 0),
        attendee_count=1,
        is_all_day=False,
        source_modified_at=datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC),
    )
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-ical",
            "summary": "Team Sync",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T10:30:00Z", "timeZone": "UTC"},
            "organizer": {"email": "host@example.com", "displayName": "Host"},
            "attendees": [{"email": "buyer@example.com"}],
        },
        "google",
    )

    assert has_meaningful_event_changes(existing, normalized) is False


def test_change_detector_ignores_missing_google_conference_data_on_sparse_payload() -> None:
    """Missing conferenceData means "not sent this cycle", not "clear the link"."""
    existing = SimpleNamespace(
        title="Team Sync",
        description=None,
        location=None,
        status="confirmed",
        conference_link="https://meet.google.com/abc-defg-hij",
        organiser_email="host@example.com",
        organiser_name="Host",
        online_meeting_provider=None,
        online_meeting_id=None,
        calendar_external_id=None,
        timezone="UTC",
        start_at=datetime(2024, 1, 15, 10, 0, 0),
        end_at=datetime(2024, 1, 15, 10, 30, 0),
        attendee_count=1,
        is_all_day=False,
        source_modified_at=datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC),
    )
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-ical",
            "summary": "Team Sync",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T10:30:00Z", "timeZone": "UTC"},
            "organizer": {"email": "host@example.com", "displayName": "Host"},
            "attendees": [{"email": "buyer@example.com"}],
        },
        "google",
    )

    assert "conference_link" not in normalized.fields
    assert has_meaningful_event_changes(existing, normalized) is False


def test_change_detector_detects_new_google_conference_link() -> None:
    """A provider-sent conference link should update rows missing that link."""
    existing = SimpleNamespace(
        title="Team Sync",
        description=None,
        location=None,
        status="confirmed",
        conference_link=None,
        organiser_email="host@example.com",
        organiser_name="Host",
        online_meeting_provider=None,
        online_meeting_id=None,
        calendar_external_id=None,
        timezone="UTC",
        start_at=datetime(2024, 1, 15, 10, 0, 0),
        end_at=datetime(2024, 1, 15, 10, 30, 0),
        attendee_count=1,
        is_all_day=False,
        source_modified_at=datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC),
    )
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-ical",
            "summary": "Team Sync",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T10:30:00Z", "timeZone": "UTC"},
            "organizer": {"email": "host@example.com", "displayName": "Host"},
            "attendees": [{"email": "buyer@example.com"}],
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/new-link"}
                ]
            },
        },
        "google",
    )

    assert has_meaningful_event_changes(existing, normalized) is True


def test_change_detector_detects_changed_google_conference_link() -> None:
    """A provider-sent replacement conference link is a meaningful change."""
    existing = SimpleNamespace(
        title="Team Sync",
        description=None,
        location=None,
        status="confirmed",
        conference_link="https://meet.google.com/old-link",
        organiser_email="host@example.com",
        organiser_name="Host",
        online_meeting_provider=None,
        online_meeting_id=None,
        calendar_external_id=None,
        timezone="UTC",
        start_at=datetime(2024, 1, 15, 10, 0, 0),
        end_at=datetime(2024, 1, 15, 10, 30, 0),
        attendee_count=1,
        is_all_day=False,
        source_modified_at=datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC),
    )
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-ical",
            "summary": "Team Sync",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2024-01-15T10:30:00Z", "timeZone": "UTC"},
            "organizer": {"email": "host@example.com", "displayName": "Host"},
            "attendees": [{"email": "buyer@example.com"}],
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/new-link"}
                ]
            },
        },
        "google",
    )

    assert has_meaningful_event_changes(existing, normalized) is True
