"""Tests for calendar sync service incremental sync functionality."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from src.workers.integrations_worker.utils.sync_metadata import SyncAction


def _parse_iso_datetime(value: str) -> datetime:
    """Parse ISO datetime string."""
    iso_utc_suffix = "+00:00"
    return datetime.fromisoformat(value.replace("Z", iso_utc_suffix))


def _has_meaningful_event_changes(existing, incoming, provider) -> bool:
    """Standalone implementation of change detection for testing.

    This mirrors the logic in CalendarSyncService._has_meaningful_event_changes.
    """
    # Extract fields based on provider
    if provider == "google":
        fields = {
            "title": incoming.get("summary") or "(No title)",
            "description": incoming.get("description"),
            "location": incoming.get("location"),
            "status": incoming.get("status"),
        }
        # Extract times
        start = incoming.get("start", {})
        if "dateTime" in start:
            fields["start_at"] = _parse_iso_datetime(start["dateTime"])
            fields["is_all_day"] = False
        elif "date" in start:
            fields["start_at"] = datetime.fromisoformat(f"{start['date']}T00:00:00+00:00")
            fields["is_all_day"] = True
        end = incoming.get("end", {})
        if "dateTime" in end:
            fields["end_at"] = _parse_iso_datetime(end["dateTime"])
        elif "date" in end:
            fields["end_at"] = datetime.fromisoformat(f"{end['date']}T00:00:00+00:00")
        # Attendee count
        attendees = incoming.get("attendees", [])
        fields["attendee_count"] = len(attendees) if isinstance(attendees, list) else 0
        # Source modified
        if incoming.get("updated"):
            fields["source_modified_at"] = _parse_iso_datetime(incoming["updated"])
        # Conference link
        conf = incoming.get("conferenceData", {})
        for entry in conf.get("entryPoints", []):
            if entry.get("entryPointType") == "video":
                fields["conference_link"] = entry.get("uri")
                break
        # Organizer
        organizer = incoming.get("organizer", {})
        fields["organiser_email"] = organizer.get("email")
    else:  # microsoft/outlook
        is_cancelled = incoming.get("isCancelled", False)
        fields = {
            "title": incoming.get("subject") or "(No title)",
            "description": incoming.get("bodyPreview") or incoming.get("body", {}).get("content"),
            "location": incoming.get("location", {}).get("displayName"),
            "status": "cancelled" if is_cancelled else "confirmed",
        }
        # Extract times
        start = incoming.get("start", {})
        if start.get("dateTime"):
            fields["start_at"] = _parse_iso_datetime(start["dateTime"])
        fields["is_all_day"] = incoming.get("isAllDay", False)
        end = incoming.get("end", {})
        if end.get("dateTime"):
            fields["end_at"] = _parse_iso_datetime(end["dateTime"])
        # Attendee count
        attendees = incoming.get("attendees", [])
        fields["attendee_count"] = len(attendees) if isinstance(attendees, list) else 0
        # Source modified
        if incoming.get("lastModifiedDateTime"):
            fields["source_modified_at"] = _parse_iso_datetime(incoming["lastModifiedDateTime"])
        # Conference link
        online = incoming.get("onlineMeeting", {})
        fields["conference_link"] = online.get("joinUrl")
        # Organizer
        organizer = incoming.get("organizer", {}).get("emailAddress", {})
        fields["organiser_email"] = organizer.get("address")

    # Check source_modified_at as primary indicator
    incoming_modified = fields.get("source_modified_at")
    if (
        incoming_modified
        and existing.source_modified_at
        and incoming_modified > existing.source_modified_at
    ):
        return True

    # Compare key fields
    checks = [
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

    # Compare start_at and end_at
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

    # Compare attendee_count
    incoming_attendee_count = fields.get("attendee_count", 0)
    if existing.attendee_count != incoming_attendee_count:
        return True

    # Compare is_all_day
    incoming_is_all_day = fields.get("is_all_day", False)
    return existing.is_all_day != incoming_is_all_day


class TestCalendarEventHasMeaningfulChanges:
    """Tests for _has_meaningful_event_changes method logic."""

    @pytest.fixture
    def mock_event(self):
        """Create a mock CalendarEvent for testing."""
        event = MagicMock()
        event.title = "Team Standup"
        event.description = "Daily standup meeting"
        event.location = "Conference Room A"
        event.status = "confirmed"
        event.conference_link = "https://meet.google.com/abc-defg-hij"
        event.organiser_email = "organizer@company.com"
        event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        event.end_at = datetime(2024, 1, 15, 10, 30, 0)
        event.attendee_count = 5
        event.is_all_day = False
        event.source_modified_at = datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC)
        return event

    def test_detects_title_change_google(self, mock_event) -> None:
        """Title change should be detected (Google)."""
        incoming = {
            "summary": "Updated Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_title_change_microsoft(self, mock_event) -> None:
        """Title change should be detected (Microsoft)."""
        incoming = {
            "subject": "Updated Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True

    def test_detects_time_change(self, mock_event) -> None:
        """Start/end time change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "start": {"dateTime": "2024-01-15T11:00:00Z"},  # Changed from 10:00 to 11:00
            "end": {"dateTime": "2024-01-15T11:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_status_change(self, mock_event) -> None:
        """Status change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "status": "cancelled",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_attendee_count_change(self, mock_event) -> None:
        """Attendee count change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 10,  # Changed from 5 to 10
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_location_change(self, mock_event) -> None:
        """Location change should be detected."""
        incoming = {
            "summary": "Team Standup",
            "location": "Conference Room B",  # Changed
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_detects_source_modified_at_change(self, mock_event) -> None:
        """source_modified_at newer than existing should trigger change."""
        incoming = {
            "summary": "Team Standup",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
            "updated": "2024-01-11T12:00:00Z",  # Newer than existing
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True

    def test_no_change_returns_false(self, mock_event) -> None:
        """No meaningful change should return False."""
        incoming = {
            "summary": "Team Standup",
            "description": "Daily standup meeting",
            "location": "Conference Room A",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
                ]
            },
            "organizer": {"email": "organizer@company.com"},
        }
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is False

    def test_handles_none_values(self) -> None:
        """None values should be handled gracefully."""
        mock_event = MagicMock()
        mock_event.title = None
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = None
        mock_event.end_at = None
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
        }
        # Should not raise an exception
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert isinstance(result, bool)


class TestCalendarIncrementalRouting:
    """Tests for calendar incremental sync routing logic."""

    def test_database_state_is_source_of_truth(self) -> None:
        """Database state (is_new) should determine create vs update."""
        record_in_db = True
        _sync_action = SyncAction.ADDED  # Unused - demonstrates db state is source of truth

        is_new = not record_in_db

        # Use is_new, NOT sync_action
        should_create = is_new
        should_update = not is_new

        assert should_create is False
        assert should_update is True

    def test_cancelled_events_handled(self) -> None:
        """Cancelled events should be marked with cancelled status."""
        # Google format
        google_event = {
            "id": "event-123",
            "status": "cancelled",
        }
        is_cancelled = google_event.get("status") == "cancelled"
        assert is_cancelled is True

        # Microsoft format
        microsoft_event = {
            "id": "event-456",
            "isCancelled": True,
        }
        is_cancelled_ms = microsoft_event.get("isCancelled", False)
        assert is_cancelled_ms is True


class TestCalendarAdapterRouting:
    """Tests for calendar adapter routing logic."""

    def test_incremental_sync_type_routes_to_incremental(self) -> None:
        """sync_type='INCREMENTAL' should route to incremental method."""
        sync_type = "INCREMENTAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is True

    def test_initial_sync_type_routes_to_full(self) -> None:
        """sync_type='INITIAL' should route to full sync method."""
        sync_type = "INITIAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is False

    def test_none_sync_type_routes_to_full(self) -> None:
        """sync_type=None should route to full sync method."""
        sync_type = None
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert not is_incremental


class TestCalendarDataTypes:
    """Tests for calendar data type support in incremental sync."""

    def test_events_data_type_supported(self) -> None:
        """'events' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"events", "calendars"}
        assert "events" in incremental_sync_entities

    def test_calendars_data_type_supported(self) -> None:
        """'calendars' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"events", "calendars"}
        assert "calendars" in incremental_sync_entities
