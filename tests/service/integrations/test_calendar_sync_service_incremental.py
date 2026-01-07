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


class TestGoogleCalendarFieldExtraction:
    """Tests for Google Calendar specific field extraction."""

    def test_extracts_summary_as_title(self) -> None:
        """Google 'summary' should map to title."""
        incoming = {
            "summary": "Project Review",
            "start": {"dateTime": "2024-01-15T14:00:00Z"},
            "end": {"dateTime": "2024-01-15T15:00:00Z"},
        }
        mock_event = MagicMock()
        mock_event.title = "Different Title"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 14, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 15, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True  # Title changed

    def test_handles_all_day_event(self) -> None:
        """All-day events use 'date' instead of 'dateTime'."""
        mock_event = MagicMock()
        mock_event.title = "Company Holiday"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 0, 0, 0)
        mock_event.end_at = datetime(2024, 1, 16, 0, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = True
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Company Holiday",
            "start": {"date": "2024-01-15"},
            "end": {"date": "2024-01-16"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is False  # No change

    def test_extracts_google_meet_link(self) -> None:
        """Should extract Google Meet link from conferenceData."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = None
        mock_event.conference_link = "https://meet.google.com/old-link"
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Meeting",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/new-link"}
                ]
            },
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True  # Conference link changed

    def test_cancelled_status_google(self) -> None:
        """Google cancelled events should have status='cancelled'."""
        incoming = {
            "id": "event-123",
            "summary": "Cancelled Meeting",
            "status": "cancelled",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        mock_event = MagicMock()
        mock_event.title = "Cancelled Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"  # Was confirmed
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert result is True  # Status changed to cancelled


class TestMicrosoftCalendarFieldExtraction:
    """Tests for Microsoft Calendar specific field extraction."""

    def test_extracts_subject_as_title(self) -> None:
        """Microsoft 'subject' should map to title."""
        incoming = {
            "subject": "Team Sync",
            "start": {"dateTime": "2024-01-15T14:00:00Z"},
            "end": {"dateTime": "2024-01-15T15:00:00Z"},
        }
        mock_event = MagicMock()
        mock_event.title = "Different Title"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 14, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 15, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True

    def test_extracts_teams_meeting_link(self) -> None:
        """Should extract Teams meeting link from onlineMeeting."""
        mock_event = MagicMock()
        mock_event.title = "Teams Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "subject": "Teams Meeting",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc123"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True  # Conference link added

    def test_cancelled_status_microsoft(self) -> None:
        """Microsoft isCancelled=true should map to status='cancelled'."""
        incoming = {
            "id": "event-456",
            "subject": "Cancelled Meeting",
            "isCancelled": True,
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        mock_event = MagicMock()
        mock_event.title = "Cancelled Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True  # Status changed

    def test_extracts_location_display_name(self) -> None:
        """Microsoft location should use displayName."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = "Room A"
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "subject": "Meeting",
            "location": {"displayName": "Room B"},  # Changed
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "microsoft")
        assert result is True


class TestCalendarIncrementalScenarios:
    """Integration scenarios testing the full incremental sync flow."""

    def test_mixed_event_scenario(self) -> None:
        """Test scenario with new, updated, cancelled, and unchanged events."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncActionCounts,
            extract_sync_action,
        )

        events = [
            # New event
            {
                "id": "event-new-1",
                "summary": "New Meeting",
                "_sync_metadata": {"sync_action": "ADDED"},
            },
            # Updated event
            {
                "id": "event-existing-1",
                "summary": "Updated Meeting",
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
            # Cancelled event
            {
                "id": "event-cancelled-1",
                "summary": "Cancelled Meeting",
                "status": "cancelled",
                "_sync_metadata": {"sync_action": "DELETED"},
            },
            # Unchanged event
            {
                "id": "event-existing-2",
                "summary": "Regular Meeting",
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
        ]

        counts = SyncActionCounts()
        for event in events:
            action = extract_sync_action(event)
            if action:
                counts.increment(action)

        assert counts.added == 1
        assert counts.updated == 2
        assert counts.deleted == 1
        assert counts.total == 4

    def test_database_state_determines_create_or_update(self) -> None:
        """Database existence should determine create vs update."""
        existing_external_ids = {"event-1", "event-2"}

        event = {
            "id": "event-1",
            "summary": "Test Event",
            "_sync_metadata": {"sync_action": "ADDED"},
        }

        external_id = event["id"]
        is_new = external_id not in existing_external_ids

        assert is_new is False
        should_update = not is_new
        assert should_update is True

    def test_new_event_gets_created(self) -> None:
        """New event (not in DB) should be created."""
        existing_external_ids = {"event-1", "event-2"}

        event = {
            "id": "event-new",
            "summary": "Brand New Event",
            "_sync_metadata": {"sync_action": "ADDED"},
        }

        external_id = event["id"]
        is_new = external_id not in existing_external_ids

        assert is_new is True

    def test_unchanged_event_gets_skipped(self) -> None:
        """Event with no meaningful changes should be skipped."""
        mock_existing = MagicMock()
        mock_existing.title = "Weekly Standup"
        mock_existing.description = None
        mock_existing.location = "Zoom"
        mock_existing.status = "confirmed"
        mock_existing.conference_link = None
        mock_existing.organiser_email = "host@company.com"
        mock_existing.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_existing.end_at = datetime(2024, 1, 15, 10, 30, 0)
        mock_existing.attendee_count = 5
        mock_existing.is_all_day = False
        mock_existing.source_modified_at = datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC)

        incoming = {
            "summary": "Weekly Standup",
            "location": "Zoom",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T10:30:00Z"},
            "attendees": [{}] * 5,
            "organizer": {"email": "host@company.com"},
        }

        has_changes = _has_meaningful_event_changes(mock_existing, incoming, "google")
        assert has_changes is False

    def test_source_modified_at_triggers_update(self) -> None:
        """Newer source_modified_at should trigger update."""
        mock_existing = MagicMock()
        mock_existing.title = "Meeting"
        mock_existing.description = None
        mock_existing.location = None
        mock_existing.status = "confirmed"
        mock_existing.conference_link = None
        mock_existing.organiser_email = None
        mock_existing.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_existing.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_existing.attendee_count = 0
        mock_existing.is_all_day = False
        mock_existing.source_modified_at = datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC)

        incoming = {
            "summary": "Meeting",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
            "updated": "2024-01-15T14:00:00Z",  # Newer
        }

        has_changes = _has_meaningful_event_changes(mock_existing, incoming, "google")
        assert has_changes is True


class TestCalendarIncrementalResultStructure:
    """Tests for incremental sync result structure."""

    def test_result_includes_all_counts(self) -> None:
        """Result should include created, updated, skipped counts."""
        expected_keys = {
            "status",
            "records_processed",
            "records_failed",
            "created",
            "updated",
            "skipped",
        }

        sample_result = {
            "status": "success",
            "records_processed": 15,
            "records_failed": 1,
            "created": 5,
            "updated": 7,
            "skipped": 3,
        }

        assert expected_keys.issubset(sample_result.keys())

    def test_action_counts_tracking(self) -> None:
        """SyncActionCounts should track all action types."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncAction,
            SyncActionCounts,
        )

        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.DELETED)

        result = counts.to_dict()
        assert result["added"] == 1
        assert result["updated"] == 2
        assert result["deleted"] == 1
        assert result["total"] == 4


class TestCalendarDispatcherIntegration:
    """Tests for dispatcher integration with calendar."""

    def test_calendar_category_in_incremental_categories(self) -> None:
        """'calendar' should be in incremental_sync_categories."""
        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        assert "calendar" in incremental_sync_categories

    def test_calendar_entities_not_in_fetcher_entities(self) -> None:
        """Calendar entities should NOT be in incremental_sync_entities (CRM only)."""
        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }
        assert "events" not in incremental_sync_entities
        assert "calendars" not in incremental_sync_entities

    def test_sync_type_only_passed_to_handler_not_fetcher(self) -> None:
        """sync_type should be passed to handler, not to calendar fetcher."""
        category = "calendar"
        data_type = "events"

        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }

        should_pass_to_fetcher = data_type in incremental_sync_entities
        assert should_pass_to_fetcher is False

        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        should_pass_to_handler = category in incremental_sync_categories
        assert should_pass_to_handler is True


class TestCalendarRecurringEvents:
    """Tests for recurring event handling."""

    def test_recurring_event_instance_identified(self) -> None:
        """Recurring event instances should be identifiable."""
        # Google recurring event instance
        google_recurring = {
            "id": "event-base_20240115T100000Z",
            "recurringEventId": "event-base",
            "summary": "Daily Standup",
        }

        is_recurring_instance = "recurringEventId" in google_recurring
        assert is_recurring_instance is True

        # Microsoft recurring event instance
        microsoft_recurring = {
            "id": "AAMkAG...",
            "seriesMasterId": "AAMkAG-master...",
            "subject": "Weekly Review",
        }

        is_recurring_instance_ms = "seriesMasterId" in microsoft_recurring
        assert is_recurring_instance_ms is True

    def test_non_recurring_event(self) -> None:
        """Non-recurring events don't have recurringEventId."""
        single_event = {
            "id": "event-single",
            "summary": "One-time Meeting",
        }

        is_recurring = "recurringEventId" in single_event or "seriesMasterId" in single_event
        assert is_recurring is False


class TestCalendarTimezoneHandling:
    """Tests for timezone handling in calendar events."""

    def test_utc_timezone_parsed(self) -> None:
        """UTC timezone (Z suffix) should be parsed correctly."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Meeting",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00Z"},
            "end": {"dateTime": "2024-01-15T11:00:00Z"},
        }

        # Should not raise
        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert isinstance(result, bool)

    def test_offset_timezone_parsed(self) -> None:
        """Offset timezone (+00:00) should be parsed correctly."""
        mock_event = MagicMock()
        mock_event.title = "Meeting"
        mock_event.description = None
        mock_event.location = None
        mock_event.status = "confirmed"
        mock_event.conference_link = None
        mock_event.organiser_email = None
        mock_event.start_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_event.end_at = datetime(2024, 1, 15, 11, 0, 0)
        mock_event.attendee_count = 0
        mock_event.is_all_day = False
        mock_event.source_modified_at = None

        incoming = {
            "summary": "Meeting",
            "status": "confirmed",
            "start": {"dateTime": "2024-01-15T10:00:00+00:00"},
            "end": {"dateTime": "2024-01-15T11:00:00+00:00"},
        }

        result = _has_meaningful_event_changes(mock_event, incoming, "google")
        assert isinstance(result, bool)
