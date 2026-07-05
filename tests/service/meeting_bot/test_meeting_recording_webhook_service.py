"""Tests for RecallMeetingRecordingWebhookService."""

from __future__ import annotations

import logging
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
async def test_transcript_done_fetches_transcript_artifact(monkeypatch, caplog) -> None:
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
            None,
        )
    )
    monkeypatch.setattr(service, "_fetch_media_artifacts", fetch_mock)
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

    with caplog.at_level(logging.INFO, logger="src.service.meeting_bot.webhook_service"):
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
    processed_logs = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "recall_webhook_processed"
    ]
    assert len(processed_logs) == 1
    assert processed_logs[0].recall_event == "transcript.done"
    assert processed_logs[0].provider_session_id == "bot_123"
    assert processed_logs[0].organisation_id == str(organisation_id)
    assert processed_logs[0].calendar_event_id == str(updated.calendar_event_id)
    assert processed_logs[0].normalized_status == "COMPLETED"
    assert processed_logs[0].transcript_status == "READY"
    assert processed_logs[0].transcript_materialized is True
    assert processed_logs[0].summary_jobs_created == 1
    assert processed_logs[0].summary_jobs_skipped == 0


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
    monkeypatch.setattr(service, "_fetch_media_artifacts", fetch_mock)

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

    fetch_mock = AsyncMock(return_value=({"id": "bot_123", "recordings": []}, None, None))
    monkeypatch.setattr(service, "_fetch_media_artifacts", fetch_mock)

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
async def test_video_mixed_done_fetches_video_artifact(monkeypatch) -> None:
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
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    updated = MeetingRecordingSession.model_validate(existing.model_dump())
    service.session_service.apply_webhook_update.return_value = updated
    fetch_mock = AsyncMock(
        return_value=(
            {"id": "bot_123", "recordings": [{"id": "rec_1"}]},
            None,
            {
                "recording_id": "rec_1",
                "video_id": "video_1",
                "download_url": "https://download.example/video.mp4",
            },
        )
    )
    monkeypatch.setattr(service, "_fetch_media_artifacts", fetch_mock)
    archive_mock = AsyncMock(
        return_value={
            "recording_id": "rec_1",
            "video_id": "video_1",
            "download_url": "https://download.example/video.mp4",
            "archived_blob_path": "meeting_recordings/event/rec_1/video_1.mp4",
        }
    )
    monkeypatch.setattr(service, "_archive_video_artifact", archive_mock)

    result = await service.process_recall_webhook(
        {
            "event": "video_mixed.done",
            "data": {
                "data": {"code": "done", "updated_at": "2026-04-23T12:40:00Z"},
                "bot": {"id": "bot_123"},
                "recording": {"id": "rec_1"},
                "video_mixed": {"id": "video_1"},
            },
        }
    )

    assert result["status"] == "processed"
    fetch_mock.assert_awaited_once_with("bot_123")
    archive_mock.assert_awaited_once_with(
        existing.calendar_event_id,
        {
            "recording_id": "rec_1",
            "video_id": "video_1",
            "download_url": "https://download.example/video.mp4",
        },
    )
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] is None
    assert kwargs["transcript_status"] is None
    assert kwargs["provider_payload"]["video_artifact"] == {
        "recording_id": "rec_1",
        "video_id": "video_1",
        "download_url": "https://download.example/video.mp4",
        "archived_blob_path": "meeting_recordings/event/rec_1/video_1.mp4",
    }


@pytest.mark.asyncio
async def test_archive_video_artifact_downloads_to_org_blob(monkeypatch) -> None:
    organisation_id = uuid4()
    event_id = uuid4()
    uploaded = {}

    class _FakeStorage:
        def __init__(self, storage_org_id):
            assert storage_org_id == organisation_id

        async def upload_stream(self, blob_path, chunks, *, content_type):
            uploaded["blob_path"] = blob_path
            uploaded["content_type"] = content_type
            uploaded["body"] = b"".join([chunk async for chunk in chunks])

        async def close(self):
            uploaded["closed"] = True

    class _FakeResponse:
        headers = {"content-type": "video/mp4", "content-length": "7"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"mp"
            yield b"4data"

    class _FakeStreamContext:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, *_args):
            return None

    class _FakeClient:
        def __init__(self, **_kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://download.example/video.mp4"
            return _FakeStreamContext()

    service = _make_service(organisation_id)
    monkeypatch.setattr(
        "src.service.meeting_bot.webhook_service.AzureOrganisationBlobStorage",
        _FakeStorage,
    )
    monkeypatch.setattr("src.service.meeting_bot.webhook_service.httpx.AsyncClient", _FakeClient)

    result = await service._archive_video_artifact(
        event_id,
        {
            "recording_id": "rec_1",
            "video_id": "video_1",
            "download_url": "https://download.example/video.mp4",
        },
    )

    assert uploaded == {
        "blob_path": f"meeting_recordings/{event_id}/rec_1/video_1.mp4",
        "content_type": "video/mp4",
        "body": b"mp4data",
        "closed": True,
    }
    assert result["archived_blob_path"] == f"meeting_recordings/{event_id}/rec_1/video_1.mp4"
    assert result["archived_content_type"] == "video/mp4"
    assert result["archived_size_bytes"] == 7


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
async def test_recording_permission_denied_marks_session_failed() -> None:
    """A bot that joins but cannot record should not stay in processing state."""
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
        "event": "bot.recording_permission_denied",
        "data": {
            "data": {
                "code": "recording_permission_denied",
                "sub_code": "host_denied",
                "updated_at": "2026-04-23T12:05:00Z",
            },
            "bot": {"id": "bot_123"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["normalized_status"] == "FAILED"
    assert result["transcript_status"] == "FAILED"
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] == MeetingRecordingStatus.FAILED
    assert kwargs["transcript_status"] == MeetingTranscriptStatus.FAILED
    assert kwargs["status_message"] == "host_denied"


@pytest.mark.asyncio
async def test_bot_fatal_before_join_marks_transcript_failed() -> None:
    """A pre-join Recall fatal should release the UI from processing."""
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.JOINING,
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
        "event": "bot.fatal",
        "data": {
            "data": {
                "code": "fatal",
                "sub_code": "meeting_not_accessible",
                "updated_at": "2026-04-23T12:05:00Z",
            },
            "bot": {"id": "bot_123"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["normalized_status"] == "FAILED"
    assert result["transcript_status"] == "FAILED"
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] == MeetingRecordingStatus.FAILED
    assert kwargs["transcript_status"] == MeetingTranscriptStatus.FAILED
    assert kwargs["status_message"] == "meeting_not_accessible"


@pytest.mark.asyncio
async def test_bot_done_after_prejoin_fatal_does_not_mark_completed() -> None:
    """Recall can emit done after fatal; without recording evidence it must stay failed."""
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.FAILED,
        transcript_status=MeetingTranscriptStatus.FAILED,
        provider_session_id="bot_123",
        provider_payload={"id": "bot_123"},
    )
    service = _make_service(organisation_id)
    service.session_service.get_by_provider_session_id.return_value = existing
    service.session_service.apply_webhook_update.return_value = existing

    payload = {
        "event": "bot.done",
        "data": {
            "data": {"code": "done", "updated_at": "2026-04-23T12:06:00Z"},
            "bot": {"id": "bot_123"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["normalized_status"] == "FAILED"
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] is None
    assert kwargs["transcript_status"] is None


@pytest.mark.asyncio
async def test_call_ended_before_recording_marks_failed() -> None:
    """Leaving from waiting room should not look like a completed recording."""
    organisation_id = uuid4()
    existing = MeetingRecordingSession(
        organisation_id=organisation_id,
        calendar_event_id=uuid4(),
        provider=MeetingRecordingProvider.RECALL_AI,
        dedupe_key="key",
        meeting_url="https://teams.example/join",
        join_at=datetime.now(UTC) + timedelta(minutes=10),
        status=MeetingRecordingStatus.JOINING,
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
        "event": "bot.call_ended",
        "data": {
            "data": {
                "code": "call_ended",
                "sub_code": "bot_kicked_from_waiting_room",
                "updated_at": "2026-04-23T12:06:00Z",
            },
            "bot": {"id": "bot_123"},
        },
    }

    result = await service.process_recall_webhook(payload)

    assert result["normalized_status"] == "FAILED"
    assert result["transcript_status"] == "FAILED"
    kwargs = service.session_service.apply_webhook_update.await_args.kwargs
    assert kwargs["status"] == MeetingRecordingStatus.FAILED
    assert kwargs["transcript_status"] == MeetingTranscriptStatus.FAILED
    assert kwargs["status_message"] == "bot_kicked_from_waiting_room"


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
    assert job.job_metadata["send_summary_notification"] is True
    assert job.job_metadata["summary_scope"] == "meeting"
