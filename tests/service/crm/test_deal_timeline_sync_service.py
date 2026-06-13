"""Tests for DealTimelineSyncService builder functions.

Tests the standalone builder functions that create timeline events:
- build_email_timeline_event
- build_meeting_timeline_event
- build_stage_change_timeline_event
- build_deal_created_timeline_event

These are pure functions that don't require database access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    StakeholderSource,
    StakeholderStance,
)
from src.domain.models.organisation_tables.sync.timeline_models import (
    TimelineEventSource,
    TimelineEventType,
)
from src.service.crm.timeline.deal_timeline_sync_service import (
    build_deal_created_timeline_event,
    build_email_timeline_event,
    build_meeting_timeline_event,
    build_stage_change_timeline_event,
    build_stakeholder_timeline_event,
)

# =============================================================================
# Mock Models for Testing
# =============================================================================


class MockDealEmailAssociation:
    """Mock DealEmailAssociation for testing."""

    def __init__(
        self,
        association_id=None,
        internet_message_id="test-msg-123",
        email_subject: str | None = "Test Email Subject",
        email_date=None,
        direction="INBOUND",
        status="APPROVED",
        created_at=None,
    ):
        self.association_id = association_id or uuid4()
        self.internet_message_id = internet_message_id
        self.email_subject = email_subject
        self.email_date = email_date or datetime.now(UTC)
        self.direction = direction
        self.status = status
        self.created_at = created_at or datetime.now(UTC)


class MockEmail:
    """Mock Email for testing."""

    def __init__(
        self,
        email_id=None,
        subject="Test Email",
        received_at=None,
        from_name="John Doe",
        from_email="john@example.com",
        to_emails=None,
        cc_emails=None,
        has_attachments=False,
        thread_id="thread-123",
    ):
        self.email_id = email_id or uuid4()
        self.subject = subject
        self.received_at = received_at or datetime.now(UTC)
        self.from_name = from_name
        self.from_email = from_email
        self.to_emails = to_emails or ["recipient@example.com"]
        self.cc_emails = cc_emails or []
        self.has_attachments = has_attachments
        self.thread_id = thread_id


class MockCalendarEventDealAssociation:
    """Mock CalendarEventDealAssociation for testing."""

    def __init__(
        self,
        association_id=None,
        event_id=None,
        status="APPROVED",
        meeting_prep_status=None,
        meeting_summary_status=None,
        created_at=None,
    ):
        self.association_id = association_id or uuid4()
        self.event_id = event_id or uuid4()
        self.status = status
        self.meeting_prep_status = meeting_prep_status
        self.meeting_summary_status = meeting_summary_status
        self.created_at = created_at or datetime.now(UTC)


class MockCalendarEvent:
    """Mock CalendarEvent for testing."""

    def __init__(
        self,
        event_id=None,
        title="Test Meeting",
        start_at=None,
        end_at=None,
        location="Conference Room A",
        conference_link="https://zoom.us/j/123",
        attendee_count=3,
        organiser_name="Jane Smith",
        organiser_email="jane@example.com",
        attendees=None,
        online_meeting_provider=None,
    ):
        self.event_id = event_id or uuid4()
        self.title = title
        self.start_at = start_at or datetime.now(UTC)
        self.end_at = end_at or (self.start_at + timedelta(hours=1))
        self.location = location
        self.conference_link = conference_link
        self.attendee_count = attendee_count
        self.organiser_name = organiser_name
        self.organiser_email = organiser_email
        self.attendees = attendees or []
        self.online_meeting_provider = online_meeting_provider


class MockAttendee:
    """Mock attendee for testing."""

    def __init__(self, display_name=None, email=None):
        self.display_name = display_name
        self.email = email


class MockCRMDeal:
    """Mock CRMDeal for testing."""

    def __init__(
        self,
        deal_id=None,
        name: str | None = "Test Deal",
        stage_name="Discovery",
        pipeline_name="Sales Pipeline",
        owner_id=None,
        amount=10000.0,
        currency="USD",
        company_name="Acme Corp",
        company_domain="acme.example",
        owner_name="Owner User",
        primary_contact_name="Primary Contact",
        primary_contact_email="primary@example.com",
        source_created_at=None,
        created_at=None,
    ):
        self.deal_id = deal_id or uuid4()
        self.name = name
        self.stage_name = stage_name
        self.pipeline_name = pipeline_name
        self.owner_id = owner_id
        self.amount = amount
        self.currency = currency
        self.company_name = company_name
        self.company_domain = company_domain
        self.owner_name = owner_name
        self.primary_contact_name = primary_contact_name
        self.primary_contact_email = primary_contact_email
        self.source_created_at = source_created_at
        self.created_at = created_at or datetime.now(UTC)


# =============================================================================
# build_email_timeline_event Tests
# =============================================================================


class TestBuildEmailTimelineEvent:
    """Tests for build_email_timeline_event function."""

    def test_basic_email_event_creation(self) -> None:
        """Should create a valid email timeline event."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation()

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert event.organisation_id == org_id
        assert event.deal_id == deal_id
        assert event.event_type == TimelineEventType.EMAIL
        assert event.source_record_id == str(assoc.association_id)
        assert event.title == "Test Email Subject"

    def test_email_event_with_email_record(self) -> None:
        """Should use email record data when available."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation(email_subject="Association Subject")
        email = MockEmail(subject="Real Email Subject", from_name="Alice")

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            email=email,
        )

        assert event.title == "Real Email Subject"
        assert event.subtitle == "From: Alice"
        assert "Alice" in event.participant_names

    def test_email_event_source_resolution(self) -> None:
        """Should use provided event source."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation()

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            event_source=TimelineEventSource.EMAIL_OUTLOOK,
        )

        assert event.event_source == TimelineEventSource.EMAIL_OUTLOOK

    def test_email_event_default_source(self) -> None:
        """Should default to EMAIL_GMAIL when no source provided."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation()

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert event.event_source == TimelineEventSource.EMAIL_GMAIL

    def test_email_event_no_subject_fallback(self) -> None:
        """Should use fallback when no subject available."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation(email_subject=None)

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert event.title == "(No Subject)"

    def test_email_event_participant_count(self) -> None:
        """Should calculate participant count correctly."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation()
        email = MockEmail(
            to_emails=["a@test.com", "b@test.com"],
            cc_emails=["c@test.com"],
        )

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            email=email,
        )

        # 1 sender + 2 to + 1 cc = 4
        assert event.participant_count == 4

    def test_email_event_metadata(self) -> None:
        """Should include metadata correctly."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation(internet_message_id="msg-456")
        email = MockEmail(has_attachments=True, thread_id="thread-789")

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            email=email,
        )

        assert event.event_metadata["internet_message_id"] == "msg-456"
        assert event.event_metadata["has_attachments"] is True
        assert event.event_metadata["thread_id"] == "thread-789"

    def test_email_event_direction_and_status(self) -> None:
        """Should capture direction and status from association."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockDealEmailAssociation(direction="OUTBOUND", status="PENDING")

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert event.direction == "OUTBOUND"
        assert event.status == "PENDING"


# =============================================================================
# build_meeting_timeline_event Tests
# =============================================================================


class TestBuildMeetingTimelineEvent:
    """Tests for build_meeting_timeline_event function."""

    def test_basic_meeting_event_creation(self) -> None:
        """Should create a valid meeting timeline event."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert event.organisation_id == org_id
        assert event.deal_id == deal_id
        assert event.event_type == TimelineEventType.MEETING
        assert event.source_record_id == str(assoc.association_id)
        assert event.title == "Meeting"  # No calendar event provided

    def test_meeting_event_with_calendar_event(self) -> None:
        """Should use calendar event data when available."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()
        cal_event = MockCalendarEvent(
            title="Sales Demo",
            organiser_name="Bob Smith",
        )

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            event=cal_event,
        )

        assert event.title == "Sales Demo"
        assert "Bob Smith" in event.participant_names

    def test_meeting_event_duration_calculation(self) -> None:
        """Should calculate duration correctly."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()
        start = datetime.now(UTC)
        cal_event = MockCalendarEvent(
            start_at=start,
            end_at=start + timedelta(minutes=45),
        )

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            event=cal_event,
        )

        assert event.subtitle == "Duration: 45 mins"
        assert event.event_metadata.get("duration_minutes") == 45

    def test_meeting_event_source_resolution(self) -> None:
        """Should use provided event source."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            event_source=TimelineEventSource.CALENDAR_OUTLOOK,
        )

        assert event.event_source == TimelineEventSource.CALENDAR_OUTLOOK

    def test_meeting_event_default_source(self) -> None:
        """Should default to CALENDAR_GOOGLE when no source provided."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert event.event_source == TimelineEventSource.CALENDAR_GOOGLE

    def test_meeting_event_with_attendees(self) -> None:
        """Should include attendee names in participant list."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()
        attendees = [
            MockAttendee(display_name="Alice", email="alice@test.com"),
            MockAttendee(display_name="Bob", email="bob@test.com"),
        ]
        cal_event = MockCalendarEvent(attendees=attendees)

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            event=cal_event,
        )

        assert "Alice" in event.participant_names
        assert "Bob" in event.participant_names

    def test_meeting_event_metadata(self) -> None:
        """Should include metadata correctly."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation(
            meeting_prep_status="COMPLETED",
            meeting_summary_status="PENDING",
        )
        cal_event = MockCalendarEvent(
            location="Room 101",
            conference_link="https://meet.google.com/abc",
        )

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            event=cal_event,
        )

        assert event.event_metadata.get("location") == "Room 101"
        assert event.event_metadata.get("conference_link") == "https://meet.google.com/abc"
        assert event.event_metadata.get("meeting_prep_status") == "COMPLETED"
        assert event.event_metadata.get("meeting_summary_status") == "PENDING"

    def test_meeting_event_no_direction(self) -> None:
        """Meetings should not have direction."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert event.direction is None


# =============================================================================
# build_stakeholder_timeline_event Tests
# =============================================================================


class TestBuildStakeholderTimelineEvent:
    """Tests for participant-backed stakeholder timeline events."""

    def test_stakeholder_event_uses_participant_id_as_public_stakeholder_id(self) -> None:
        org_id = uuid4()
        integration_id = uuid4()
        deal_id = uuid4()
        participant = CRMDealParticipant(
            organisation_id=org_id,
            integration_id=integration_id,
            deal_id=deal_id,
            participant_type=CRMParticipantType.CONTACT,
            related_name="Alex Buyer",
            related_email="alex@example.com",
            job_title="CFO",
            participant_source=StakeholderSource.EMAIL_DISCOVERY.value,
            approval_status=ApprovalStatus.APPROVED.value,
            stance=StakeholderStance.CHAMPION.value,
            decision_power="DECISION_MAKER",
        )

        event = build_stakeholder_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            stakeholder=participant,
        )

        assert event.source_record_id == str(participant.participant_id)
        assert event.status == ApprovalStatus.APPROVED.value
        assert event.event_metadata["stakeholder_id"] == str(participant.participant_id)
        assert event.event_metadata["participant_id"] == str(participant.participant_id)
        assert event.event_metadata["email"] == "alex@example.com"
        assert event.event_metadata["name"] == "Alex Buyer"
        assert event.event_metadata["source"] == StakeholderSource.EMAIL_DISCOVERY.value
        assert event.participant_names == ["Alex Buyer"]


# =============================================================================
# build_stage_change_timeline_event Tests
# =============================================================================


class TestBuildStageChangeTimelineEvent:
    """Tests for build_stage_change_timeline_event function."""

    def test_basic_stage_change_event(self) -> None:
        """Should create a valid stage change timeline event."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "history-123",
            "from_stage_id": "stage-1",
            "stage_id": "stage-2",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert event.organisation_id == org_id
        assert event.deal_id == deal_id
        assert event.event_type == TimelineEventType.STAGE_CHANGE
        assert event.source_record_id == "history-123"

    def test_stage_change_title_with_both_stages(self) -> None:
        """Should format title with both old and new stages."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "from_stage_id": "stage-1",
            "stage_id": "stage-2",
            "from_stage_label": "Discovery",
            "stage_label": "Proposal",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert "Discovery" in event.title
        assert "Proposal" in event.title
        assert "→" in event.title

    def test_stage_change_title_without_from_stage(self) -> None:
        """Should format title without arrow when no from_stage."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "from_stage_id": None,
            "stage_id": "stage-1",
            "stage_label": "Discovery",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert event.title == "Stage: Discovery"
        assert "→" not in event.title

    def test_stage_change_uses_labels_when_available(self) -> None:
        """Should use display labels over stage IDs."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "from_stage_id": "id-1",
            "stage_id": "id-2",
            "from_stage_label": "Initial Contact",
            "stage_label": "Qualified Lead",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert "Initial Contact" in event.title
        assert "Qualified Lead" in event.title

    def test_stage_change_event_source(self) -> None:
        """Should use provided event source."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "stage_id": "stage-1",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
            event_source=TimelineEventSource.CRM_SALESFORCE,
        )

        assert event.event_source == TimelineEventSource.CRM_SALESFORCE

    def test_stage_change_default_source(self) -> None:
        """Should default to CRM_HUBSPOT when no source provided."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "stage_id": "stage-1",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert event.event_source == TimelineEventSource.CRM_HUBSPOT

    def test_stage_change_metadata(self) -> None:
        """Should include metadata with stage info."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "from_stage_id": "old-stage",
            "stage_id": "new-stage",
            "from_stage_label": "Old Label",
            "stage_label": "New Label",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert event.event_metadata["old_stage_id"] == "old-stage"
        assert event.event_metadata["new_stage_id"] == "new-stage"
        assert event.event_metadata["old_stage_label"] == "Old Label"
        assert event.event_metadata["new_stage_label"] == "New Label"

    def test_stage_change_no_participants(self) -> None:
        """Stage changes should have no participants."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "stage_id": "stage-1",
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert event.participant_names == []
        assert event.participant_count == 0


# =============================================================================
# build_deal_created_timeline_event Tests
# =============================================================================


class TestBuildDealCreatedTimelineEvent:
    """Tests for build_deal_created_timeline_event function."""

    def test_basic_deal_created_event(self) -> None:
        """Should create a valid deal created timeline event."""
        org_id = uuid4()
        deal = MockCRMDeal(name="Big Enterprise Deal")

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.organisation_id == org_id
        assert event.deal_id == deal.deal_id
        assert event.event_type == TimelineEventType.DEAL_CREATED
        assert event.source_record_id == str(deal.deal_id)
        assert "Big Enterprise Deal" in event.title

    def test_deal_created_title_format(self) -> None:
        """Should format title correctly."""
        org_id = uuid4()
        deal = MockCRMDeal(name="Test Deal")

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.title == "Deal Created: Test Deal"

    def test_deal_created_without_name(self) -> None:
        """Should handle deal without name."""
        org_id = uuid4()
        deal = MockCRMDeal(name=None)

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.title == "Deal Created"

    def test_deal_created_subtitle_with_stage(self) -> None:
        """Should include initial stage in subtitle."""
        org_id = uuid4()
        deal = MockCRMDeal(stage_name="Qualification")

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.subtitle == "Initial Stage: Qualification"

    def test_deal_created_uses_source_created_at(self) -> None:
        """Should prefer source_created_at over created_at."""
        org_id = uuid4()
        source_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        sync_time = datetime(2024, 1, 20, 10, 0, 0, tzinfo=UTC)
        deal = MockCRMDeal(
            source_created_at=source_time,
            created_at=sync_time,
        )

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.event_timestamp == source_time

    def test_deal_created_fallback_to_created_at(self) -> None:
        """Should fall back to created_at when source_created_at is None."""
        org_id = uuid4()
        sync_time = datetime(2024, 1, 20, 10, 0, 0, tzinfo=UTC)
        deal = MockCRMDeal(
            source_created_at=None,
            created_at=sync_time,
        )

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.event_timestamp == sync_time

    def test_deal_created_event_source(self) -> None:
        """Should use provided event source."""
        org_id = uuid4()
        deal = MockCRMDeal()

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
            event_source=TimelineEventSource.CRM_SALESFORCE,
        )

        assert event.event_source == TimelineEventSource.CRM_SALESFORCE

    def test_deal_created_default_source(self) -> None:
        """Should default to CRM_HUBSPOT when no source provided."""
        org_id = uuid4()
        deal = MockCRMDeal()

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.event_source == TimelineEventSource.CRM_HUBSPOT

    def test_deal_created_metadata(self) -> None:
        """Should include metadata with deal info."""
        org_id = uuid4()
        owner_id = uuid4()
        deal = MockCRMDeal(
            stage_name="Discovery",
            pipeline_name="Enterprise Sales",
            owner_id=owner_id,
            amount=50000.0,
        )

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.event_metadata["initial_stage"] == "Discovery"
        assert event.event_metadata["pipeline_name"] == "Enterprise Sales"
        assert event.event_metadata["owner_id"] == str(owner_id)
        assert event.event_metadata["amount"] == 50000.0

    def test_deal_created_no_participants(self) -> None:
        """Deal created events should have no participants."""
        org_id = uuid4()
        deal = MockCRMDeal()

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert event.participant_names == []
        assert event.participant_count == 0


# =============================================================================
# Title Truncation Tests
# =============================================================================


class TestTitleTruncation:
    """Tests for title/subtitle truncation (max 500 chars)."""

    def test_email_title_truncation(self) -> None:
        """Email title should be truncated to 500 chars."""
        org_id = uuid4()
        deal_id = uuid4()
        long_subject = "A" * 600
        assoc = MockDealEmailAssociation(email_subject=long_subject)

        event = build_email_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
        )

        assert len(event.title) == 500

    def test_meeting_title_truncation(self) -> None:
        """Meeting title should be truncated to 500 chars."""
        org_id = uuid4()
        deal_id = uuid4()
        assoc = MockCalendarEventDealAssociation()
        cal_event = MockCalendarEvent(title="B" * 600)

        event = build_meeting_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            association=assoc,
            event=cal_event,
        )

        assert len(event.title) == 500

    def test_stage_change_title_truncation(self) -> None:
        """Stage change title should be truncated to 500 chars."""
        org_id = uuid4()
        deal_id = uuid4()
        history_entry = {
            "history_id": "h1",
            "stage_label": "C" * 600,
            "entered_at": datetime.now(UTC),
        }

        event = build_stage_change_timeline_event(
            organisation_id=org_id,
            deal_id=deal_id,
            history_entry=history_entry,
        )

        assert len(event.title) == 500

    def test_deal_created_title_truncation(self) -> None:
        """Deal created title should be truncated to 500 chars."""
        org_id = uuid4()
        deal = MockCRMDeal(name="D" * 600)

        event = build_deal_created_timeline_event(
            organisation_id=org_id,
            deal=deal,
        )

        assert len(event.title) == 500
