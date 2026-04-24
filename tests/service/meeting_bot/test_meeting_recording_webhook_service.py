"""Tests for RecallMeetingRecordingWebhookService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.service.meeting_bot.webhook_service import RecallMeetingRecordingWebhookService


def _make_service(organisation_id):
    # Bypass the real constructor so each test can inject a mocked session service.
    svc = RecallMeetingRecordingWebhookService.__new__(RecallMeetingRecordingWebhookService)
    svc.organisation_id = organisation_id
    svc.session_service = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_bot_in_call_recording_updates_session_status(monkeypatch) -> None:
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.SENT,
        transcript_status=MeetingTranscriptStatus.PENDING,
        provider_session_id="bot_123",
        provider_payload={"id": "bot_123"},
    )
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    service.session_service.apply_webhook_update.return_value = existing

    payload = {
        "event": "bot.in_call_recording",
        "data": {
            "data": {"code": "in_call_recording", "updated_at": "2026-04-23T12:00:00Z"},
            "bot": {"id": "bot_123"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["status"] == "processed"
    service.session_service.apply_webhook_update.assert_awaited_once()
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] == MeetingRecordingStatus.IN_CALL
    assert kwargs["transcript_status"] is None
    assert kwargs["provider_status"] == "in_call_recording"


@pytest.mark.asyncio
async def test_transcript_done_fetches_transcript_artifact(monkeypatch) -> None:
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.COMPLETED,
        transcript_status=MeetingTranscriptStatus.PENDING,
        provider_session_id="bot_123",
        provider_payload={"id": "bot_123"},
    )
    updated = MeetingRecordingSession.model_validate(existing.model_dump())
    updated.transcript_status = MeetingTranscriptStatus.READY
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    service.session_service.apply_webhook_update.return_value = updated

    fetch_mock = AsyncMock(
        return_value=(
            {
                "id": "bot_123",
                "recordings": [
                    {
                        "id": "rec_1",
                        "media_shortcuts": {
                            "transcript": {
                                "id": "transcript_1",
                                "data": {"download_url": "https://download.example/transcript"},
                            }
                        },
                    }
                ],
            },
            {
                "recording_id": "rec_1",
                "transcript_id": "transcript_1",
                "download_url": "https://download.example/transcript",
            },
        )
    )
    monkeypatch.setattr(service, "_fetch_transcript_artifact", fetch_mock)

    payload = {
        "event": "transcript.done",
        "data": {
            "data": {"code": "done", "updated_at": "2026-04-23T12:30:00Z"},
            "bot": {"id": "bot_123"},
            "transcript": {"id": "transcript_1"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["status"] == "processed"
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["transcript_status"] == MeetingTranscriptStatus.READY
    assert kwargs["transcript_ready_at"] == datetime(2026, 4, 23, 12, 30, tzinfo=UTC)
    assert kwargs["provider_payload"]["transcript_artifact"]["transcript_id"] == "transcript_1"


@pytest.mark.asyncio
async def test_extract_organisation_id_reads_bot_metadata() -> None:
    organisation_id = str(uuid4())

    result = RecallMeetingRecordingWebhookService.extract_organisation_id(
        {
            "data": {
                "bot": {
                    "metadata": {"organisation_id": organisation_id},
                }
            }
        }
    )

    assert result == organisation_id
