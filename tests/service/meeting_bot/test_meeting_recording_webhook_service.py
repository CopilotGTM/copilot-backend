"""Tests for RecallMeetingRecordingWebhookService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
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


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarResult(self._rows)


class _SummaryJobSession:
    def __init__(self, execute_rows):
        self._execute_rows = list(execute_rows)
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        rows = self._execute_rows.pop(0) if self._execute_rows else []
        return _ExecuteResult(rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


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
async def test_webhook_response_accepts_string_backed_status_columns(monkeypatch) -> None:
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
    updated = MeetingRecordingSession.model_validate(existing.model_dump())
    updated.status = "IN_CALL"
    updated.transcript_status = "PENDING"
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    service.session_service.apply_webhook_update.return_value = updated

    payload = {
        "event": "bot.in_call_recording",
        "data": {
            "data": {"code": "in_call_recording", "updated_at": "2026-04-23T12:00:00Z"},
            "bot": {"id": "bot_123"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["normalized_status"] == "IN_CALL"
    assert result["transcript_status"] == "PENDING"


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
    materialize_mock = AsyncMock(
        return_value={
            "transcript_materialized": True,
            "summary_jobs_created": 1,
            "summary_jobs_skipped": 0,
        }
    )
    service._materialize_recall_transcript_and_enqueue_summaries = materialize_mock

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
    materialize_mock.assert_awaited_once_with(updated.calendar_event_id)
    assert result["transcript_materialized"] is True
    assert result["summary_jobs_created"] == 1
    assert result["summary_jobs_skipped"] == 0


@pytest.mark.asyncio
async def test_recording_done_updates_status_without_fetching_transcript(monkeypatch) -> None:
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.IN_CALL,
        transcript_status=MeetingTranscriptStatus.PENDING,
        provider_session_id="bot_123",
        provider_payload={"id": "bot_123"},
    )
    updated = MeetingRecordingSession.model_validate(existing.model_dump())
    updated.status = MeetingRecordingStatus.COMPLETED
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    service.session_service.apply_webhook_update.return_value = updated

    fetch_mock = AsyncMock()
    monkeypatch.setattr(service, "_fetch_transcript_artifact", fetch_mock)

    payload = {
        "event": "recording.done",
        "data": {
            "data": {"code": "done", "updated_at": "2026-04-23T12:35:00Z"},
            "bot": {"id": "bot_123"},
            "recording": {"id": "rec_1"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["status"] == "processed"
    fetch_mock.assert_not_awaited()
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] == MeetingRecordingStatus.COMPLETED
    assert kwargs["transcript_status"] is None
    assert kwargs["transcript_ready_at"] is None
    assert "transcript_artifact" not in kwargs["provider_payload"]


@pytest.mark.asyncio
async def test_recording_done_preserves_transcript_status(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.IN_CALL,
        transcript_status=MeetingTranscriptStatus.PENDING,
        provider_session_id="bot_123",
        provider_payload={"id": "bot_123"},
    )
    updated = MeetingRecordingSession.model_validate(existing.model_dump())
    updated.status = MeetingRecordingStatus.COMPLETED
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    service.session_service.apply_webhook_update.return_value = updated

    fetch_mock = AsyncMock(return_value=({"id": "bot_123", "recordings": []}, None))
    monkeypatch.setattr(service, "_fetch_transcript_artifact", fetch_mock)

    payload = {
        "event": "recording.done",
        "data": {
            "data": {"code": "done", "updated_at": "2026-04-23T12:35:00Z"},
            "bot": {"id": "bot_123"},
            "recording": {"id": "rec_1"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["status"] == "processed"
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] == MeetingRecordingStatus.COMPLETED
    assert kwargs["transcript_status"] is None
    assert kwargs["transcript_ready_at"] is None
    assert "transcript_artifact" not in kwargs["provider_payload"]


@pytest.mark.asyncio
async def test_recording_failed_marks_session_and_transcript_failed(monkeypatch) -> None:
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.IN_CALL,
        transcript_status=MeetingTranscriptStatus.PENDING,
        provider_session_id="bot_123",
        provider_payload={"id": "bot_123"},
    )
    updated = MeetingRecordingSession.model_validate(existing.model_dump())
    updated.status = MeetingRecordingStatus.FAILED
    updated.transcript_status = MeetingTranscriptStatus.FAILED
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    service.session_service.apply_webhook_update.return_value = updated

    payload = {
        "event": "recording.failed",
        "data": {
            "data": {
                "code": "failed",
                "sub_code": "recording_unavailable",
                "message": "Recording failed",
                "updated_at": "2026-04-23T12:35:00Z",
            },
            "bot": {"id": "bot_123"},
            "recording": {"id": "rec_1"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["status"] == "processed"
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] == MeetingRecordingStatus.FAILED
    assert kwargs["transcript_status"] == MeetingTranscriptStatus.FAILED
    assert kwargs["status_message"] == "recording_unavailable: Recording failed"


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


@pytest.mark.asyncio
async def test_create_summary_jobs_creates_non_deal_job_when_no_active_association(
    monkeypatch,
) -> None:
    """Recall transcripts should still generate a meeting summary when no deal is linked."""
    organisation_id = uuid4()
    event_id = uuid4()
    service = _make_service(organisation_id)
    session = _SummaryJobSession(
        [
            [],  # active deal associations
            [],  # active non-deal summary jobs
        ]
    )

    async def fake_acquire_org_session(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.service.meeting_bot.webhook_service.acquire_org_async_session",
        fake_acquire_org_session,
    )

    result = await service._create_meeting_summary_jobs(event_id)

    assert result["created"] == 1
    assert result["skipped"] == 0
    assert len(result["job_ids_to_enqueue"]) == 1
    assert len(session.added) == 1
    job = session.added[0]
    assert job.deal_id is None
    assert job.event_id == event_id
    assert job.job_type == DealJobType.MEETING_SUMMARY
    assert job.status == JobStatus.PENDING
    assert job.job_metadata["source"] == "recall_webhook"
    assert job.job_metadata["summary_scope"] == "meeting"
