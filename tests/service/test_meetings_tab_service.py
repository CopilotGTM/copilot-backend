"""Focused regression tests for Meetings tab service edge cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.api.schemas.meetings_tab_models import MeetingLinkedRecordType, MeetingsView
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus, GenerationStatus
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

    linked = _service()._linked_record(assoc, deal)  # noqa: SLF001

    assert linked is not None
    assert linked.record_type == MeetingLinkedRecordType.DEAL
    assert linked.record_id == deal_id
    assert linked.name == "Acme Corp"


def test_meetings_query_does_not_depend_on_removed_link_tables() -> None:
    compiled = str(_service()._meeting_rows_stmt().compile())  # noqa: SLF001

    assert "meeting_record_links" not in compiled
    assert "meeting_bot_preferences" not in compiled
    assert "calendar_event_deal_associations" in compiled


def test_deal_association_surfaces_deal_summary_state() -> None:
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


@pytest.mark.asyncio
async def test_mirror_deal_link_reactivates_without_removing_other_deal_associations() -> None:
    existing_assoc = SimpleNamespace(
        status=AssociationStatus.REMOVED,
        match_score=0.2,
        updated_at=datetime.now(UTC) - timedelta(days=1),
    )

    class _Result:
        def scalar_one_or_none(self):
            return existing_assoc

    class _Session:
        def __init__(self):
            self.added = []

        async def execute(self, _stmt):
            return _Result()

        def add(self, item):
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
    before = datetime.now(UTC)

    start, end = _service()._resolve_window(MeetingsView.RECORDINGS, None, None)  # noqa: SLF001

    after = datetime.now(UTC)
    assert start < before - timedelta(days=179)
    assert end > after


@pytest.mark.asyncio
async def test_deal_recording_detail_returns_deal_summary(monkeypatch) -> None:
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
