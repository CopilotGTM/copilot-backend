"""Focused regression tests for Meetings tab service edge cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.api.schemas.meetings_tab_models import MeetingLinkedRecordType, MeetingsView
from src.domain.models.organisation_tables.sync.calendar import GenerationStatus
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
    deal_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        name="Acme Corp",
        stage_name="Proposal",
        amount=80000,
    )
    assoc = SimpleNamespace(deal_id=deal_id)

    linked = _service()._linked_record(None, assoc, deal, None, None)  # noqa: SLF001

    assert linked is not None
    assert linked.record_type == MeetingLinkedRecordType.DEAL
    assert linked.record_id == deal_id
    assert linked.name == "Acme Corp"


def test_explicit_lead_link_wins_over_existing_deal_association() -> None:
    lead_id = uuid4()
    link = SimpleNamespace(record_type="LEAD")
    assoc = SimpleNamespace(deal_id=uuid4())
    deal = SimpleNamespace(
        deal_id=assoc.deal_id,
        name="Legacy deal",
        stage_name="Proposal",
        amount=100,
    )
    lead = SimpleNamespace(
        lead_id=lead_id,
        full_name="Rohan Mehta",
        company_name="FinEdge Systems",
        email="rohan@example.com",
        status="NEW",
    )

    linked = _service()._linked_record(link, assoc, deal, lead, None)  # noqa: SLF001

    assert linked is not None
    assert linked.record_type == MeetingLinkedRecordType.LEAD
    assert linked.record_id == lead_id


def test_lead_link_does_not_surface_stale_deal_summary_state() -> None:
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
    link = SimpleNamespace(record_type="LEAD")
    assoc = SimpleNamespace(
        deal_id=uuid4(),
        meeting_summary_status=GenerationStatus.COMPLETED,
        meeting_prep_status=GenerationStatus.COMPLETED,
    )
    lead = SimpleNamespace(
        lead_id=uuid4(),
        full_name="Rohan Mehta",
        company_name="FinEdge Systems",
        email="rohan@example.com",
        status="NEW",
    )
    row = (event, link, assoc, None, lead, None, None, None, None)

    item = _service()._format_row(row, {"example.com"})  # noqa: SLF001

    assert item.linked_record is not None
    assert item.linked_record.record_type == MeetingLinkedRecordType.LEAD
    assert item.has_summary is False
    assert item.has_prep is False
    assert item.next_action is None


def test_recordings_window_includes_today_by_default() -> None:
    before = datetime.now(UTC)

    start, end = _service()._resolve_window(MeetingsView.RECORDINGS, None, None)  # noqa: SLF001

    after = datetime.now(UTC)
    assert start < before - timedelta(days=179)
    assert end > after


@pytest.mark.asyncio
async def test_lead_recording_detail_does_not_return_stale_deal_summary(monkeypatch) -> None:
    service = _service()

    async def _rollback() -> None:
        return None

    service.session.rollback = _rollback
    now = datetime.now(UTC)
    event = SimpleNamespace(event_id=uuid4())
    link = SimpleNamespace(record_type="LEAD")
    assoc = SimpleNamespace(
        deal_id=uuid4(),
        meeting_summary_data={"summary": "deal-only"},
        meeting_followup_data={"action_items": [{"title": "deal action"}]},
    )
    lead = SimpleNamespace(
        lead_id=uuid4(),
        full_name="Rohan Mehta",
        company_name="FinEdge Systems",
        email="rohan@example.com",
        status="NEW",
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
        return (event, link, assoc, None, lead, None, None, recording, None)

    monkeypatch.setattr(service, "_get_meeting_row", _get_row)

    detail = await service.get_recording(event.event_id)

    assert detail.playback_url == "https://example.com/video.mp4"
    assert detail.summary is None
    assert detail.action_items == []
