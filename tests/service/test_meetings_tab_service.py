"""Focused regression tests for Meetings tab service edge cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.api.schemas.meetings_tab_models import (
    MeetingBotStatus,
    MeetingDetail,
    MeetingLinkedRecordType,
    MeetingsView,
)
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.service.calendar_invites.models import CalendarInviteAttendee
from src.service.integrations.calendar.integration_types import (
    DISPATCH_CALENDAR_INTEGRATION_TYPES,
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES,
)
from src.service.meetings_tab_service import CALENDAR_INTEGRATION_TYPES, MeetingsTabService


def _service() -> MeetingsTabService:
    return MeetingsTabService(
        session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
        user_timezone="UTC",
    )


def test_meetings_tab_uses_provider_parent_calendar_integrations() -> None:
    """Meetings tab must query provider-parent auth integrations, not dispatch-only children."""
    assert CALENDAR_INTEGRATION_TYPES == PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES
    assert CALENDAR_INTEGRATION_TYPES != DISPATCH_CALENDAR_INTEGRATION_TYPES


def test_existing_deal_association_is_used_as_link_fallback() -> None:
    """Existing calendar-deal associations must render as Meetings-tab link chips."""
    deal_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        name="Acme Corp",
        stage_name="Proposal",
        amount=80000,
    )
    assoc = SimpleNamespace(deal_id=deal_id)

    linked = _service()._linked_record(assoc, deal)  # noqa: SLF001

    assert linked is not None
    assert linked.record_type == MeetingLinkedRecordType.DEAL
    assert linked.record_id == deal_id
    assert linked.name == "Acme Corp"


def test_meetings_query_does_not_depend_on_removed_link_tables() -> None:
    """Meetings V1 must keep using existing calendar/deal tables only."""
    compiled = str(_service()._meeting_rows_stmt().compile())  # noqa: SLF001

    assert "meeting_record_links" not in compiled
    assert "meeting_bot_preferences" not in compiled
    assert "'outlook_calendar'" not in compiled
    assert "calendar_event_deal_associations" in compiled


def test_deal_association_surfaces_deal_summary_state() -> None:
    """Deal-linked rows should expose summary and prep completion flags."""
    now = datetime.now(UTC)
    event = SimpleNamespace(
        event_id=uuid4(),
        title="Intro call",
        start_at=now,
        end_at=now + timedelta(minutes=30),
        online_meeting_provider="google_meet",
        provider="google",
        conference_link=None,
        status="confirmed",
        attendees=[],
        attendee_count=0,
    )
    assoc = SimpleNamespace(
        deal_id=uuid4(),
        meeting_summary_status=GenerationStatus.COMPLETED,
        meeting_summary_data={"summary": "deal-only"},
        meeting_summary_generated_at=now,
        meeting_prep_status=GenerationStatus.COMPLETED,
    )
    deal = SimpleNamespace(
        deal_id=assoc.deal_id,
        name="Acme Corp",
        stage_name="Proposal",
        amount=80000,
    )
    row = (event, assoc, deal, None, None)

    item = _service()._format_row(row, {"example.com"}, include_summary_data=True)  # noqa: SLF001

    assert item.linked_record is not None
    assert item.linked_record.record_type == MeetingLinkedRecordType.DEAL
    assert item.has_summary is True
    assert item.has_prep is True
    assert item.meeting_summary_data == {"summary": "deal-only"}
    assert item.meeting_summary_status == GenerationStatus.COMPLETED


def test_meeting_list_row_omits_summary_payload_by_default() -> None:
    """List responses should expose summary state without carrying full summary content."""
    now = datetime.now(UTC)
    event = SimpleNamespace(
        event_id=uuid4(),
        title="Internal sync",
        start_at=now,
        end_at=now + timedelta(minutes=30),
        online_meeting_provider="google_meet",
        provider="google",
        conference_link=None,
        status="confirmed",
        attendees=[],
        attendee_count=0,
    )
    meeting = SimpleNamespace(
        meeting_summary_status=GenerationStatus.COMPLETED,
        meeting_summary_data={"data": {"meeting_summary": "large summary"}},
        meeting_summary_generated_at=now,
    )
    row = (event, None, None, None, meeting)

    item = _service()._format_row(row, {"example.com"})  # noqa: SLF001

    assert item.has_summary is True
    assert item.meeting_summary_status == GenerationStatus.COMPLETED
    assert item.meeting_summary_generated_at == now
    assert item.meeting_summary_data is None


def test_non_deal_meeting_surfaces_meeting_level_summary_state() -> None:
    """Non-deal meeting summaries should be visible from the Meetings tab detail payload."""
    now = datetime.now(UTC)
    event = SimpleNamespace(
        event_id=uuid4(),
        title="Internal sync",
        start_at=now,
        end_at=now + timedelta(minutes=30),
        online_meeting_provider="google_meet",
        provider="google",
        conference_link=None,
        status="confirmed",
        attendees=[],
        attendee_count=0,
    )
    meeting = SimpleNamespace(
        meeting_summary_status=GenerationStatus.COMPLETED,
        meeting_summary_data={"data": {"meeting_summary": "non-deal summary"}},
        meeting_summary_generated_at=now,
    )
    row = (event, None, None, None, meeting)

    item = _service()._format_row(row, {"example.com"}, include_summary_data=True)  # noqa: SLF001

    assert item.linked_record is None
    assert item.has_summary is True
    assert item.meeting_summary_data == {"data": {"meeting_summary": "non-deal summary"}}
    assert item.meeting_summary_status == GenerationStatus.COMPLETED
    assert item.meeting_summary_generated_at == now


def test_platform_label_mapping_uses_provider_and_link_signals() -> None:
    """Platform labels should be friendly names derived from provider or URL signals."""
    service = _service()

    assert (
        service._platform_label(  # noqa: SLF001
            SimpleNamespace(
                online_meeting_provider="googleMeet", provider="google", conference_link=None
            )
        )
        == "Google Meet"
    )
    assert (
        service._platform_label(  # noqa: SLF001
            SimpleNamespace(
                online_meeting_provider="teamsForBusiness",
                provider="microsoft",
                conference_link="https://teams.microsoft.com/l/meetup-join/abc",
            )
        )
        == "Microsoft Teams"
    )
    assert (
        service._platform_label(  # noqa: SLF001
            SimpleNamespace(
                online_meeting_provider=None,
                provider="google",
                conference_link="https://acme.zoom.us/j/123",
            )
        )
        == "Zoom"
    )
    assert (
        service._platform_label(  # noqa: SLF001
            SimpleNamespace(
                online_meeting_provider=None,
                provider="google",
                conference_link="https://acme.webex.com/meet/123",
            )
        )
        == "Webex"
    )
    assert (
        service._platform_label(  # noqa: SLF001
            SimpleNamespace(
                online_meeting_provider=None,
                provider="google",
                conference_link="https://example.com/join",
            )
        )
        == "Not available"
    )
    assert (
        service._platform_label(  # noqa: SLF001
            SimpleNamespace(
                online_meeting_provider=None,
                provider="google",
                conference_link="https://zoom.evil.example/j/123",
            )
        )
        == "Not available"
    )


def test_recording_availability_uses_recording_status_only() -> None:
    """The recording column must ignore transcript state and use recording lifecycle only."""
    service = _service()

    assert service._recording_availability(None) == "Not available"  # noqa: SLF001
    for status in (
        MeetingRecordingStatus.PENDING_SEND,
        MeetingRecordingStatus.SEND_QUEUED,
        MeetingRecordingStatus.SENT,
        MeetingRecordingStatus.JOINING,
        MeetingRecordingStatus.IN_CALL,
    ):
        assert (
            service._recording_availability(SimpleNamespace(status=status))  # noqa: SLF001
            == "Processing"
        )

    assert (
        service._recording_availability(  # noqa: SLF001
            SimpleNamespace(status=MeetingRecordingStatus.COMPLETED)
        )
        == "Available"
    )
    assert (
        service._recording_availability(SimpleNamespace(status=MeetingRecordingStatus.FAILED))  # noqa: SLF001
        == "Not available"
    )


def test_bot_action_flags_are_time_and_state_scoped() -> None:
    """Invite/remove flags should be disabled for past meetings and active bots."""
    service = _service()
    future_event = SimpleNamespace(
        start_at=datetime.now(UTC) + timedelta(hours=1),
        conference_link="https://meet.google.com/abc-defg-hij",
    )
    past_event = SimpleNamespace(
        start_at=datetime.now(UTC) - timedelta(minutes=1),
        conference_link="https://meet.google.com/abc-defg-hij",
    )
    sent_recording = SimpleNamespace(
        status=MeetingRecordingStatus.SENT,
        transcript_status=MeetingTranscriptStatus.PENDING,
        provider_payload={},
    )
    cancelled_recording = SimpleNamespace(
        status=MeetingRecordingStatus.CANCELLED,
        transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        provider_payload={},
    )
    cleanup_failed_recording = SimpleNamespace(
        status=MeetingRecordingStatus.CANCELLED,
        transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        provider_payload={"last_cancel_error": {"detail": "provider outage"}},
    )

    assert service._can_invite_bot(event=future_event, recording=None) is True  # noqa: SLF001
    assert service._can_invite_bot(event=past_event, recording=None) is False  # noqa: SLF001
    assert service._can_invite_bot(event=future_event, recording=sent_recording) is False  # noqa: SLF001
    assert service._can_invite_bot(event=future_event, recording=cancelled_recording) is True  # noqa: SLF001
    assert (
        service._can_invite_bot(event=future_event, recording=cleanup_failed_recording) is False  # noqa: SLF001
    )
    assert service._can_remove_bot(future_event, sent_recording) is True  # noqa: SLF001
    assert service._can_remove_bot(past_event, sent_recording) is False  # noqa: SLF001


def test_failed_bot_status_projects_reason_not_already_sent() -> None:
    """Failed provider sessions should tell the UI what failed."""
    service = _service()
    now = datetime.now(UTC)
    event = SimpleNamespace(
        start_at=now + timedelta(hours=1),
        conference_link="https://meet.google.com/abc-defg-hij",
    )
    recording = SimpleNamespace(
        status=MeetingRecordingStatus.FAILED,
        transcript_status=MeetingTranscriptStatus.FAILED,
        provider_payload={},
        provider_session_id="bot_123",
        sent_to_provider_at=now,
        status_message="meeting_not_accessible",
    )

    bot = service._bot_status(event, recording, None, True)  # noqa: SLF001

    assert bot.status == "failed"
    assert bot.reason == "meeting_not_accessible"
    assert bot.can_invite is True
    assert bot.can_remove is True


@pytest.mark.asyncio
async def test_mirror_deal_link_reactivates_without_removing_other_deal_associations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual linking reactivates one association without enforcing a single active deal."""
    event = CalendarEvent(
        organisation_id=uuid4(),
        event_id=uuid4(),
        external_id="event-1",
        title="Customer sync",
        start_at=datetime.now(UTC) + timedelta(hours=1),
        end_at=datetime.now(UTC) + timedelta(hours=2),
    )
    existing_assoc = SimpleNamespace(
        association_id=uuid4(),
        organisation_id=event.organisation_id,
        event_id=event.event_id,
        deal_id=uuid4(),
        status=AssociationStatus.REMOVED,
        match_score=0.2,
        updated_at=datetime.now(UTC) - timedelta(days=1),
    )

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            """Return the scripted row for each service lookup."""
            return self._value

    class _Session:
        def __init__(self):
            self.added = []
            self.results = [existing_assoc, event]

        async def execute(self, _stmt):
            """Mimic the async session execute contract used by the link mirror."""
            return _Result(self.results.pop(0))

        def add(self, item):
            """Capture unexpected inserts so the test can assert no duplicate row."""
            self.added.append(item)

    service = _service()
    fake_session = _Session()
    service.session = fake_session
    projected = []

    class _ProjectionService:
        def __init__(self, projected_session, projected_org_id):  # noqa: ANN001
            assert projected_session is fake_session
            assert projected_org_id == service.organisation_id

        async def upsert_meeting_association(self, association, event_record):  # noqa: ANN001
            """Record that manual link mirroring refreshes the feed projection."""
            projected.append((association, event_record))

    monkeypatch.setattr(
        "src.service.meetings_tab_service.ActivityFeedProjectionService",
        _ProjectionService,
    )

    await service._mirror_deal_link(event.event_id, existing_assoc.deal_id)  # noqa: SLF001

    assert existing_assoc.status == AssociationStatus.ACTIVE
    assert existing_assoc.match_score == 1.0
    assert existing_assoc.updated_at > datetime.now(UTC) - timedelta(seconds=5)
    assert fake_session.added == []
    assert projected == [(existing_assoc, event)]


def test_recordings_window_includes_today_by_default() -> None:
    """The recordings tab should include current-day sessions by default."""
    before = datetime.now(UTC)

    start, end = _service()._resolve_window(MeetingsView.RECORDINGS, None, None)  # noqa: SLF001

    after = datetime.now(UTC)
    assert start < before - timedelta(days=179)
    assert end > after


@pytest.mark.asyncio
async def test_deal_recording_detail_returns_deal_summary(monkeypatch) -> None:
    """Recording detail should use deal-scoped summary data when the meeting is linked."""
    service = _service()

    async def _rollback() -> None:
        return None

    service.session.rollback = _rollback
    now = datetime.now(UTC)
    event = SimpleNamespace(event_id=uuid4())
    assoc = SimpleNamespace(
        deal_id=uuid4(),
        meeting_summary_data={"summary": "deal-only"},
        meeting_followup_data={"action_items": [{"title": "deal action"}]},
    )
    deal = SimpleNamespace(
        deal_id=assoc.deal_id,
        name="Acme Corp",
        stage_name="Proposal",
        amount=80000,
    )
    recording = SimpleNamespace(
        status="DONE",
        transcript_status="DONE",
        provider_session_id=None,
        provider_payload={
            "bot_snapshot": {
                "recordings": [
                    {
                        "media_shortcuts": {
                            "video_mixed": {
                                "id": "video_123",
                                "data": {
                                    "download_url": "https://example.com/video.mp4",
                                    "expires_at": now.isoformat(),
                                },
                            }
                        }
                    }
                ]
            }
        },
    )

    async def _get_row(_event_id):
        return (event, assoc, deal, recording, None)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)

    detail = await service.get_recording(event.event_id)

    assert detail.playback_url == "https://example.com/video.mp4"
    assert detail.summary == {"summary": "deal-only"}
    assert detail.action_items == [{"title": "deal action"}]


@pytest.mark.asyncio
async def test_recording_detail_falls_back_to_meeting_level_summary(monkeypatch) -> None:
    """Recording detail should show meeting-level summary when there is no deal association."""
    service = _service()

    async def _rollback() -> None:
        return None

    service.session.rollback = _rollback
    now = datetime.now(UTC)
    event = SimpleNamespace(event_id=uuid4())
    meeting = SimpleNamespace(
        meeting_summary_data={"data": {"meeting_summary": "non-deal summary"}},
    )
    recording = SimpleNamespace(
        status="DONE",
        transcript_status="DONE",
        provider_session_id=None,
        provider_payload={
            "bot_snapshot": {
                "recordings": [
                    {
                        "media_shortcuts": {
                            "video_mixed": {
                                "id": "video_123",
                                "data": {
                                    "download_url": "https://example.com/video.mp4",
                                    "expires_at": now.isoformat(),
                                },
                            }
                        }
                    }
                ]
            }
        },
    )

    async def _get_row(_event_id):
        return (event, None, None, recording, meeting)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)

    detail = await service.get_recording(event.event_id)

    assert detail.summary == {"data": {"meeting_summary": "non-deal summary"}}


@pytest.mark.asyncio
async def test_recording_detail_returns_transcript_segments_sorted_by_index(monkeypatch) -> None:
    """Recording detail should expose compact ordered transcript segments."""
    service = _service()
    meeting_id = uuid4()

    async def _rollback() -> None:
        return None

    class _SegmentResult:
        def scalars(self):
            """Return self so the fake result supports SQLAlchemy's chained API."""
            return self

        def all(self):
            """Return ordered transcript rows for the recording-detail formatter."""
            return [
                SimpleNamespace(
                    segment_index=1,
                    start_time_seconds=12.5,
                    speaker_external_id="speaker-1",
                    speaker_name="Nikhil",
                    content="Let's review the next step.",
                ),
                SimpleNamespace(
                    segment_index=2,
                    start_time_seconds=24.0,
                    speaker_external_id=None,
                    speaker_name="Priya",
                    content="I can send that by Friday.",
                ),
            ]

    async def _execute(_stmt):
        return _SegmentResult()

    service.session.rollback = _rollback
    service.session.execute = _execute
    event = SimpleNamespace(event_id=uuid4())
    meeting = SimpleNamespace(
        meeting_id=meeting_id,
        meeting_summary_data={"data": {"meeting_summary": "non-deal summary"}},
    )
    recording = SimpleNamespace(
        status="DONE",
        transcript_status="DONE",
        provider_session_id=None,
        provider_payload={
            "bot_snapshot": {
                "recordings": [
                    {
                        "media_shortcuts": {
                            "video_mixed": {
                                "id": "video_123",
                                "data": {"download_url": "https://example.com/video.mp4"},
                            }
                        }
                    }
                ]
            }
        },
    )

    async def _get_row(_event_id):
        return (event, None, None, recording, meeting)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)

    detail = await service.get_recording(event.event_id)

    assert detail.summary == {"data": {"meeting_summary": "non-deal summary"}}
    assert detail.model_dump(mode="json")["transcript_segments"] == [
        {
            "start_time": 12.5,
            "speaker_id": "speaker-1",
            "speaker_name": "Nikhil",
            "text": "Let's review the next step.",
        },
        {
            "start_time": 24.0,
            "speaker_id": None,
            "speaker_name": "Priya",
            "text": "I can send that by Friday.",
        },
    ]


@pytest.mark.asyncio
async def test_recording_detail_returns_empty_transcript_segments_when_missing(monkeypatch) -> None:
    """Recording detail should return an empty transcript segment array when rows are absent."""
    service = _service()

    async def _rollback() -> None:
        return None

    service.session.rollback = _rollback
    now = datetime.now(UTC)
    event = SimpleNamespace(event_id=uuid4())
    recording = SimpleNamespace(
        status="DONE",
        transcript_status="DONE",
        provider_session_id=None,
        provider_payload={
            "bot_snapshot": {
                "recordings": [
                    {
                        "media_shortcuts": {
                            "video_mixed": {
                                "id": "video_123",
                                "data": {
                                    "download_url": "https://example.com/video.mp4",
                                    "expires_at": now.isoformat(),
                                },
                            }
                        }
                    }
                ]
            }
        },
    )

    async def _get_row(_event_id):
        return (event, None, None, recording, None)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)

    detail = await service.get_recording(event.event_id)

    assert detail.model_dump(mode="json")["transcript_segments"] == []


@pytest.mark.asyncio
async def test_recording_detail_uses_persisted_video_artifact(monkeypatch) -> None:
    """Playback should work from webhook-persisted video artifact without Recall I/O."""
    service = _service()

    async def _rollback() -> None:
        return None

    service.session.rollback = _rollback
    event = SimpleNamespace(event_id=uuid4())
    recording = SimpleNamespace(
        status="COMPLETED",
        transcript_status="READY",
        provider_session_id=None,
        provider_payload={
            "video_artifact": {
                "recording_id": "rec_1",
                "video_id": "video_123",
                "download_url": "https://example.com/video.mp4",
            }
        },
    )

    async def _get_row(_event_id):
        return (event, None, None, recording, None)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)

    detail = await service.get_recording(event.event_id)

    assert detail.playback_url == "https://example.com/video.mp4"


@pytest.mark.asyncio
async def test_recording_detail_uses_archived_video_sas(monkeypatch) -> None:
    """Archived videos should use our org blob SAS instead of Recall's URL."""
    service = _service()

    async def _rollback() -> None:
        return None

    service.session.rollback = _rollback
    event = SimpleNamespace(event_id=uuid4())
    recording = SimpleNamespace(
        status="COMPLETED",
        transcript_status="READY",
        provider_session_id=None,
        provider_payload={
            "video_artifact": {
                "recording_id": "rec_1",
                "video_id": "video_123",
                "download_url": "https://recall.example/video.mp4",
                "archived_blob_path": "meeting_recordings/event/rec_1/video_123.mp4",
            }
        },
    )

    class _FakeStorage:
        def __init__(self, organisation_id):
            assert organisation_id == service.organisation_id

        async def generate_read_sas(self, blob_path, *, minutes, download_filename):
            assert blob_path == "meeting_recordings/event/rec_1/video_123.mp4"
            assert minutes == 120
            assert download_filename == "meeting-recording.mp4"
            return "https://blob.example/recording.mp4?sas=1"

        async def close(self):
            return None

    async def _get_row(_event_id):
        return (event, None, None, recording, None)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)
    monkeypatch.setattr(
        "src.service.meetings_tab_service.AzureOrganisationBlobStorage",
        _FakeStorage,
    )

    detail = await service.get_recording(event.event_id)

    assert detail.playback_url == "https://blob.example/recording.mp4?sas=1"


@pytest.mark.asyncio
async def test_create_meeting_with_join_link_enqueues_single_event_bot_dispatch(monkeypatch):
    """New non-deal meetings with join links should trigger the idempotent bot task."""
    service = _service()
    event_id = uuid4()
    provider_event_id = "provider-event-123"
    conference_link = "https://meet.google.com/abc-defg-hij"
    captured_invite = {}

    class _FakeCalendarInviteService:
        def __init__(self, *_args, **_kwargs):
            return None

        async def create_invite(self, invite):
            """Return a synced provider event so create_meeting can continue."""
            captured_invite["invite"] = invite
            return SimpleNamespace(
                event_id=event_id,
                provider_event_id=provider_event_id,
                conference_link=conference_link,
            )

    monkeypatch.setattr(
        "src.service.meetings_tab_service.CalendarInviteService",
        _FakeCalendarInviteService,
    )
    detail = MeetingDetail(
        event_id=event_id,
        title="Customer sync",
        start_at=datetime.now(UTC) + timedelta(hours=2),
        end_at=datetime.now(UTC) + timedelta(hours=3),
        platform="google",
        conference_link=conference_link,
        status="confirmed",
        bot=MeetingBotStatus(status="invite_available"),
    )

    async def _get_meeting_detail(_event_id):
        return detail

    monkeypatch.setattr(service, "get_meeting_detail", _get_meeting_detail)
    send_task = AsyncMock(return_value="task-123")
    monkeypatch.setattr(
        "src.service.meetings_tab_service.send_deal_task_async",
        send_task,
    )

    result = await service.create_meeting(
        SimpleNamespace(
            title="Customer sync",
            body=None,
            location=None,
            start_at=datetime.now(UTC) + timedelta(hours=2),
            end_at=datetime.now(UTC) + timedelta(hours=3),
            timezone="UTC",
            attendees=[CalendarInviteAttendee(email="buyer@example.com")],
            link=None,
            sender_email=None,
            idempotency_key=None,
        )
    )

    assert captured_invite["invite"].deal_id is None
    assert result.event_id == event_id
    assert result.bot_dispatch == {"task_id": "task-123", "status": "queued"}
    send_task.assert_awaited_once_with(
        "deal.send_meeting_recording",
        kwargs={
            "organisation_id": str(service.organisation_id),
            "calendar_event_id": str(event_id),
        },
        queue="deal",
        routing_key="deal",
    )
