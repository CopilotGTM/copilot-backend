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


def test_microsoft_normalizer_extracts_google_meet_from_location() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Meet with Carlos",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "location": {"displayName": "https://meet.google.com/yqz-yaqx-dup"},
            "locations": [
                {
                    "displayName": "https://meet.google.com/yqz-yaqx-dup",
                    "uniqueId": "https://meet.google.com/yqz-yaqx-dup",
                    "locationType": "default",
                    "uniqueIdType": "private",
                }
            ],
            "organizer": {"emailAddress": {"address": "cmunoz@avaya.com", "name": "Carlos"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://meet.google.com/yqz-yaqx-dup"
    assert normalized.fields["online_meeting_provider"] == "googleMeet"


def test_microsoft_normalizer_extracts_zoom_from_locations() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Zoom",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "locations": [
                {
                    "displayName": "Zoom",
                    "uniqueId": "https://acme.zoom.us/j/123456789",
                    "locationType": "default",
                    "uniqueIdType": "private",
                }
            ],
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://acme.zoom.us/j/123456789"
    assert normalized.fields["online_meeting_provider"] == "zoom"


def test_microsoft_normalizer_extracts_teams_from_body_when_online_meeting_missing() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Teams",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "body": {
                "contentType": "html",
                "content": '<a href="https://teams.microsoft.com/l/meetup-join/abc">Join</a>',
            },
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://teams.microsoft.com/l/meetup-join/abc"
    assert normalized.fields["online_meeting_provider"] == "teamsForBusiness"


def test_microsoft_normalizer_extracts_webex_from_body_preview() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Webex",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "bodyPreview": "Join Webex https://acme.webex.com/meet/buyer.",
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://acme.webex.com/meet/buyer"
    assert normalized.fields["online_meeting_provider"] == "webex"


def test_google_normalizer_extracts_zoom_from_description_when_conference_data_missing() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Zoom",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "Join Zoom Meeting https://acme.zoom.us/j/987654321",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://acme.zoom.us/j/987654321"
    assert normalized.fields["online_meeting_provider"] == "zoom"


def test_google_normalizer_extracts_teams_from_location() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Teams",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "location": "https://teams.live.com/meet/123",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://teams.live.com/meet/123"
    assert normalized.fields["online_meeting_provider"] == "teamsForBusiness"


def test_google_normalizer_extracts_webex_from_description() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Webex",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "Join Webex: https://webex.com/meet/customer)",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://webex.com/meet/customer"
    assert normalized.fields["online_meeting_provider"] == "webex"


def test_google_normalizer_classifies_structured_zoom_addon_from_entry_point_uri() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Zoom",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "conferenceData": {
                "conferenceId": "987654321",
                "conferenceSolution": {"key": {"type": "addOn"}, "name": "Zoom Meeting"},
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://acme.zoom.us/j/987654321"}
                ],
            },
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://acme.zoom.us/j/987654321"
    assert normalized.fields["online_meeting_provider"] == "zoom"
    assert normalized.fields["online_meeting_id"] == "987654321"


def test_google_join_link_backfill_extracts_structured_zoom_addon() -> None:
    update = CalendarEventNormalizer.extract_join_link_update(
        "google",
        {
            "conferenceData": {
                "conferenceId": "987654321",
                "conferenceSolution": {"key": {"type": "addOn"}, "name": "Zoom Meeting"},
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://acme.zoom.us/j/987654321"}
                ],
            },
        },
    )

    assert update == {
        "conference_link": "https://acme.zoom.us/j/987654321",
        "online_meeting_provider": "zoom",
    }


def test_google_normalizer_prefers_structured_google_meet_over_description_link() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Meet",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "Backup link https://acme.zoom.us/j/987654321",
            "conferenceData": {
                "conferenceId": "abc-defg-hij",
                "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
                ],
            },
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://meet.google.com/abc-defg-hij"
    assert normalized.fields["online_meeting_provider"] == "googleMeet"
    assert normalized.fields["online_meeting_id"] == "abc-defg-hij"


def test_normalizer_rejects_unsupported_location_url() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer call",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "location": {"displayName": "https://example.com/join"},
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert "conference_link" not in normalized.fields
    assert "online_meeting_provider" not in normalized.fields


def test_normalizer_rejects_lookalike_hosts() -> None:
    for url in (
        "https://zoom.evil.example/j/123456789",
        "https://meet.google.com.evil.example/abc-defg-hij",
        "https://teams.microsoft.com.evil.example/l/meetup-join/abc",
    ):
        normalized = CalendarEventNormalizer().normalize(
            {
                "id": "ms-user-event",
                "iCalUId": f"shared-ms-ical-{url}",
                "subject": "Customer call",
                "isCancelled": False,
                "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
                "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
                "location": {"displayName": url},
                "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
                "attendees": [],
            },
            "microsoft",
        )

        assert "conference_link" not in normalized.fields
        assert "online_meeting_provider" not in normalized.fields


def test_normalizer_extracts_url_without_trailing_prose() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Customer Meet",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "description": "https://meet.google.com/abc-defg-hij Join with Google Meet",
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert normalized.fields["conference_link"] == "https://meet.google.com/abc-defg-hij"
    assert normalized.fields["online_meeting_provider"] == "googleMeet"


def test_google_normalizer_rejects_unsupported_structured_addon_url() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "google-user-event",
            "iCalUID": "shared-google-ical",
            "summary": "Unsupported add-on",
            "status": "confirmed",
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "conferenceData": {
                "conferenceId": "vendor-123",
                "conferenceSolution": {"key": {"type": "addOn"}, "name": "Unknown Vendor"},
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meetings.example.com/join/123"}
                ],
            },
            "organizer": {"email": "buyer@example.com", "displayName": "Buyer"},
            "attendees": [],
        },
        "google",
    )

    assert "conference_link" not in normalized.fields
    assert "online_meeting_provider" not in normalized.fields
    assert "online_meeting_id" not in normalized.fields


def test_normalizer_unescapes_html_query_parameters() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Zoom",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "body": {
                "contentType": "html",
                "content": (
                    '<a href="https://acme.zoom.us/j/123456789?pwd=abc&amp;from=addon">'
                    "Join Zoom</a>"
                ),
            },
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert (
        normalized.fields["conference_link"]
        == "https://acme.zoom.us/j/123456789?pwd=abc&from=addon"
    )
    assert normalized.fields["online_meeting_provider"] == "zoom"


def test_normalizer_unwraps_microsoft_safelink_to_supported_meeting_url() -> None:
    normalized = CalendarEventNormalizer().normalize(
        {
            "id": "ms-user-event",
            "iCalUId": "shared-ms-ical",
            "subject": "Customer Zoom",
            "isCancelled": False,
            "start": {"dateTime": "2026-06-11T17:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T17:45:00Z", "timeZone": "UTC"},
            "body": {
                "contentType": "html",
                "content": (
                    "Join: "
                    "https://nam12.safelinks.protection.outlook.com/?url="
                    "https%3A%2F%2Facme.zoom.us%2Fj%2F123456789%3Fpwd%3Dabc"
                    "&amp;data=05%7C02%7Copaque"
                ),
            },
            "organizer": {"emailAddress": {"address": "buyer@example.com", "name": "Buyer"}},
            "attendees": [],
        },
        "microsoft",
    )

    assert normalized.fields["conference_link"] == "https://acme.zoom.us/j/123456789?pwd=abc"
    assert normalized.fields["online_meeting_provider"] == "zoom"
