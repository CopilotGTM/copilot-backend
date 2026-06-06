"""Focused regression tests for Meetings tab service edge cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.api.schemas.meetings_tab_models import MeetingLinkedRecordType, MeetingsView
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus, GenerationStatus
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.service.meetings_tab_service import MeetingsTabService


def _service() -> MeetingsTabService:
    return MeetingsTabService(
        session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        user_id=uuid4(),
        user_timezone="UTC",
    )


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
        meeting_prep_status=GenerationStatus.COMPLETED,
    )
    deal = SimpleNamespace(
        deal_id=assoc.deal_id,
        name="Acme Corp",
        stage_name="Proposal",
        amount=80000,
    )
    row = (event, assoc, deal, None, None)

    item = _service()._format_row(row, {"example.com"})  # noqa: SLF001

    assert item.linked_record is not None
    assert item.linked_record.record_type == MeetingLinkedRecordType.DEAL
    assert item.has_summary is True
    assert item.has_prep is True


def test_platform_label_mapping_uses_provider_and_link_signals() -> None:
    """Platform labels should be friendly names derived from provider or URL signals."""
    service = _service()

    assert (
        service._platform_label(  # noqa: SLF001
            SimpleNamespace(online_meeting_provider="googleMeet", provider="google", conference_link=None)
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


@pytest.mark.asyncio
async def test_mirror_deal_link_reactivates_without_removing_other_deal_associations() -> None:
    """Manual linking reactivates one association without enforcing a single active deal."""
    existing_assoc = SimpleNamespace(
        status=AssociationStatus.REMOVED,
        match_score=0.2,
        updated_at=datetime.now(UTC) - timedelta(days=1),
    )

    class _Result:
        def scalar_one_or_none(self):
            """Return the existing association for the service lookup."""
            return existing_assoc

    class _Session:
        def __init__(self):
            self.added = []

        async def execute(self, _stmt):
            """Mimic the async session execute contract used by the link mirror."""
            return _Result()

        def add(self, item):
            """Capture unexpected inserts so the test can assert no duplicate row."""
            self.added.append(item)

    service = _service()
    fake_session = _Session()
    service.session = fake_session

    await service._mirror_deal_link(uuid4(), uuid4())  # noqa: SLF001

    assert existing_assoc.status == AssociationStatus.ACTIVE
    assert existing_assoc.match_score == 1.0
    assert existing_assoc.updated_at > datetime.now(UTC) - timedelta(seconds=5)
    assert fake_session.added == []


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
