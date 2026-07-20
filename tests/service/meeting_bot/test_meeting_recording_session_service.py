"""Tests for MeetingRecordingSessionService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.service.meeting_bot.session_service import MeetingRecordingSessionService


def _make_service():
    svc = MeetingRecordingSessionService.__new__(MeetingRecordingSessionService)
    svc.organisation_id = uuid4()
    return svc


def _fake_session_gen_with_scalar(result):
    # Mirror the single-yield `acquire_org_async_session()` pattern used in prod.
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = result
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()

    async def gen(_organisation_id):
        """Yield one mocked org session for service methods under test."""
        yield mock_session

    return mock_session, gen


def _fake_session_gen_with_scalars(items):
    # Enough of the SQLAlchemy result interface to cover `.scalars().all()`.
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = items
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()

    async def gen(_organisation_id):
        """Yield one mocked org session whose result exposes scalars().all()."""
        yield mock_session

    return mock_session, gen


class TestGetOrCreatePendingSession:
    """Coverage for session creation and refresh idempotency."""

    @pytest.mark.asyncio
    async def test_creates_pending_session_when_missing(self, monkeypatch) -> None:
        """A missing row is initialized as a Recall pending-send session."""
        service = _make_service()
        event_id = uuid4()
        join_at = datetime.now(UTC) + timedelta(minutes=5)
        session, fake_gen = _fake_session_gen_with_scalar(None)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.get_or_create_pending_session(
            calendar_event_id=event_id,
            meeting_url="https://teams.example/join",
            join_at=join_at,
        )

        assert isinstance(result, MeetingRecordingSession)
        assert result.organisation_id == service.organisation_id
        assert result.calendar_event_id == event_id
        assert result.provider == MeetingRecordingProvider.RECALL_AI
        assert result.status == MeetingRecordingStatus.PENDING_SEND
        assert result.transcript_status == MeetingTranscriptStatus.NOT_REQUESTED
        assert result.sent_to_provider_at is None
        session.add.assert_called_once()
        session.commit.assert_awaited_once()
        session.refresh.assert_awaited_once_with(result)

    @pytest.mark.asyncio
    async def test_recovers_when_concurrent_insert_wins_first_session_race(
        self, monkeypatch
    ) -> None:
        """A duplicate insert race returns the winner row instead of failing dispatch."""
        service = _make_service()
        event_id = uuid4()
        join_at = datetime.now(UTC) + timedelta(minutes=5)
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=event_id,
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="winner-key",
            meeting_url="https://teams.example/join",
            join_at=join_at,
            status=MeetingRecordingStatus.PENDING_SEND,
            transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        )
        missing_result = MagicMock()
        missing_result.scalar_one_or_none.return_value = None
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = existing
        session = MagicMock()
        session.execute = AsyncMock(side_effect=[missing_result, existing_result])
        session.commit = AsyncMock(
            side_effect=IntegrityError("insert", {}, Exception("unique violation"))
        )
        session.rollback = AsyncMock()
        session.refresh = AsyncMock()
        session.add = MagicMock()

        async def fake_gen(_organisation_id):
            yield session

        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.get_or_create_pending_session(
            calendar_event_id=event_id,
            meeting_url="https://teams.example/join",
            join_at=join_at,
        )

        assert result is existing
        session.rollback.assert_awaited_once()
        assert session.execute.await_count == 2
        session.refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_updates_existing_unsent_session(self, monkeypatch) -> None:
        """An unsent row refreshes its join target for retry correctness."""
        service = _make_service()
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="old-key",
            meeting_url="https://old.example/join",
            join_at=datetime.now(UTC) + timedelta(minutes=1),
            status=MeetingRecordingStatus.FAILED,
            transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        )
        new_join_at = datetime.now(UTC) + timedelta(minutes=10)
        session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.get_or_create_pending_session(
            calendar_event_id=existing.calendar_event_id,
            meeting_url="https://new.example/join",
            join_at=new_join_at,
        )

        assert result is existing
        assert result.meeting_url == "https://new.example/join"
        assert result.join_at == new_join_at
        assert result.status == MeetingRecordingStatus.PENDING_SEND
        assert result.sent_to_provider_at is None
        session.add.assert_called_once_with(existing)
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_session_is_not_refreshed_for_resend(self, monkeypatch) -> None:
        """A cancelled row is a durable user opt-out, not a retry candidate."""
        service = _make_service()
        original_join_at = datetime.now(UTC) + timedelta(minutes=1)
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="old-key",
            meeting_url="https://old.example/join",
            join_at=original_join_at,
            status=MeetingRecordingStatus.CANCELLED,
            transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        )
        session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.get_or_create_pending_session(
            calendar_event_id=existing.calendar_event_id,
            meeting_url="https://new.example/join",
            join_at=datetime.now(UTC) + timedelta(minutes=10),
        )

        assert result is existing
        assert result.meeting_url == "https://old.example/join"
        assert result.join_at == original_join_at
        assert result.status == MeetingRecordingStatus.CANCELLED
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_refresh_existing_sent_session_before_reschedule_decision(
        self, monkeypatch
    ) -> None:
        """A sent row preserves old target data until dispatch checks for drift."""
        service = _make_service()
        original_join_at = datetime.now(UTC) + timedelta(minutes=1)
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="old-key",
            meeting_url="https://old.example/join",
            join_at=original_join_at,
            status=MeetingRecordingStatus.SENT,
            transcript_status=MeetingTranscriptStatus.PENDING,
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_old",
        )
        session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.get_or_create_pending_session(
            calendar_event_id=existing.calendar_event_id,
            meeting_url="https://new.example/join",
            join_at=datetime.now(UTC) + timedelta(minutes=10),
        )

        assert result is existing
        assert result.meeting_url == "https://old.example/join"
        assert result.join_at == original_join_at
        assert result.status == MeetingRecordingStatus.SENT
        assert result.provider_session_id == "bot_old"
        session.add.assert_not_called()
        session.commit.assert_not_awaited()


class TestResetForResend:
    """Coverage for converting stale sent rows back to send-ready state."""

    @pytest.mark.asyncio
    async def test_reset_for_resend_clears_send_state_and_refreshes_session(
        self, monkeypatch
    ) -> None:
        """Reschedule reset keeps org/event identity and clears old bot state."""
        service = _make_service()
        original_join_at = datetime.now(UTC) + timedelta(minutes=1)
        event_id = uuid4()
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=event_id,
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://old.example/join",
            join_at=original_join_at,
            status=MeetingRecordingStatus.SENT,
            transcript_status=MeetingTranscriptStatus.READY,
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_old",
            provider_status="done",
            status_message="old status",
            transcript_ready_at=datetime.now(UTC),
            last_webhook_at=datetime.now(UTC),
            provider_payload={"old": "value"},
        )
        session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        new_join_at = datetime.now(UTC) + timedelta(minutes=10)
        result = await service.reset_for_resend(
            existing.session_id,
            meeting_url="https://new.example/join",
            join_at=new_join_at,
        )

        assert result is existing
        assert result.organisation_id == service.organisation_id
        assert result.calendar_event_id == event_id
        assert result.meeting_url == "https://new.example/join"
        assert result.join_at == new_join_at
        assert result.status == MeetingRecordingStatus.PENDING_SEND
        assert result.status_message == "Session reset for reschedule"
        assert result.sent_to_provider_at is None
        assert result.provider_session_id is None
        assert result.provider_status is None
        assert result.provider_payload == {}
        assert result.last_webhook_at is None
        assert result.transcript_ready_at is None
        assert result.transcript_status == MeetingTranscriptStatus.NOT_REQUESTED

    @pytest.mark.asyncio
    async def test_reset_for_resend_if_current_preserves_provider_cleanup_state(
        self, monkeypatch
    ) -> None:
        """A resend reset claims only the provider row the caller inspected."""
        service = _make_service()
        original_join_at = datetime.now(UTC) + timedelta(minutes=1)
        sent_at = datetime.now(UTC)
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://old.example/join",
            join_at=original_join_at,
            status=MeetingRecordingStatus.SENT,
            transcript_status=MeetingTranscriptStatus.PENDING,
            sent_to_provider_at=sent_at,
            provider_session_id="bot_old",
            provider_status="scheduled",
            provider_payload={"id": "bot_old"},
        )
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = existing.session_id
        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = existing
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[update_result, select_result])
        mock_session.commit = AsyncMock()

        async def fake_gen(_organisation_id):
            yield mock_session

        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        new_join_at = datetime.now(UTC) + timedelta(minutes=10)
        result = await service.reset_for_resend_if_current(
            existing.session_id,
            expected_provider_session_id="bot_old",
            expected_meeting_url="https://old.example/join",
            expected_join_at=original_join_at,
            expected_sent_to_provider_at=sent_at,
            meeting_url="https://new.example/join",
            join_at=new_join_at,
        )

        assert result is existing
        assert result.__dict__["_reset_claimed"] is True
        assert result.meeting_url == "https://new.example/join"
        assert result.join_at == new_join_at
        assert result.status == MeetingRecordingStatus.PENDING_SEND
        assert result.provider_session_id == "bot_old"
        assert result.provider_status == "scheduled"
        assert result.provider_payload == {"id": "bot_old"}
        assert result.sent_to_provider_at is None
        assert result.transcript_status == MeetingTranscriptStatus.NOT_REQUESTED

    @pytest.mark.asyncio
    async def test_reset_for_resend_if_current_does_not_clear_stale_provider_state(
        self, monkeypatch
    ) -> None:
        """A stale worker sees the current row but does not clear newer provider data."""
        service = _make_service()
        current_join_at = datetime.now(UTC) + timedelta(minutes=10)
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://current.example/join",
            join_at=current_join_at,
            status=MeetingRecordingStatus.SENT,
            transcript_status=MeetingTranscriptStatus.PENDING,
            sent_to_provider_at=datetime.now(UTC),
            provider_session_id="bot_current",
            provider_status="scheduled",
            provider_payload={"id": "bot_current"},
        )
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = None
        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = existing
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[update_result, select_result])
        mock_session.commit = AsyncMock()

        async def fake_gen(_organisation_id):
            yield mock_session

        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.reset_for_resend_if_current(
            existing.session_id,
            expected_provider_session_id="bot_old",
            expected_meeting_url="https://old.example/join",
            expected_join_at=current_join_at - timedelta(minutes=5),
            expected_sent_to_provider_at=datetime.now(UTC) - timedelta(minutes=1),
            meeting_url="https://new.example/join",
            join_at=current_join_at + timedelta(minutes=5),
        )

        assert result is existing
        assert result.__dict__["_reset_claimed"] is False
        assert result.meeting_url == "https://current.example/join"
        assert result.provider_session_id == "bot_current"
        assert result.sent_to_provider_at is not None


class TestStatusTransitions:
    """Coverage for provider lifecycle status transitions."""

    @pytest.mark.asyncio
    async def test_mark_send_queued_sets_send_status(self, monkeypatch) -> None:
        """Queueing a send records the in-flight state before provider I/O."""
        service = _make_service()
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://example/join",
            join_at=datetime.now(UTC) + timedelta(minutes=5),
            status=MeetingRecordingStatus.PENDING_SEND,
            transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        )
        session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.mark_send_queued(existing.session_id)

        assert result is existing
        assert result.status == MeetingRecordingStatus.SEND_QUEUED
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_send_queued_flags_stale_worker_recovery(self, monkeypatch) -> None:
        """A reclaimed queue row tells dispatch to reconcile before another create."""
        service = _make_service()
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://example/join",
            join_at=datetime.now(UTC),
            status=MeetingRecordingStatus.SEND_QUEUED,
            transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
            updated_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        _session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.mark_send_queued(existing.session_id)

        assert result is existing
        assert result.__dict__["_send_claimed"] is True
        assert result.__dict__["_recovered_stale_claim"] is True

    @pytest.mark.asyncio
    async def test_mark_sent_sets_provider_state(self, monkeypatch) -> None:
        """Provider acceptance stores bot metadata and starts transcript waiting."""
        service = _make_service()
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://example/join",
            join_at=datetime.now(UTC) + timedelta(minutes=5),
            status=MeetingRecordingStatus.SEND_QUEUED,
            transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        )
        session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.mark_sent(
            existing.session_id,
            provider_session_id="bot_123",
            provider_status="bot.created",
            provider_payload={"id": "bot_123"},
        )

        assert result is existing
        assert result.status == MeetingRecordingStatus.SENT
        assert result.provider_session_id == "bot_123"
        assert result.provider_status == "bot.created"
        assert result.provider_payload == {"id": "bot_123"}
        assert result.sent_to_provider_at is not None
        assert result.transcript_status == MeetingTranscriptStatus.PENDING
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apply_webhook_update_updates_provider_session_state(self, monkeypatch) -> None:
        """Webhook updates merge provider lifecycle data without losing row identity."""
        service = _make_service()
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://example/join",
            join_at=datetime.now(UTC) + timedelta(minutes=5),
            status=MeetingRecordingStatus.SENT,
            transcript_status=MeetingTranscriptStatus.PENDING,
            provider_session_id="bot_123",
            provider_payload={"id": "bot_123"},
        )
        session, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.apply_webhook_update(
            "bot_123",
            provider_status="done",
            provider_payload={"last_webhook_event_name": "bot.done"},
            status=MeetingRecordingStatus.COMPLETED,
            transcript_status=MeetingTranscriptStatus.READY,
            status_message="completed",
            transcript_ready_at=datetime.now(UTC),
        )

        assert result is existing
        assert result.status == MeetingRecordingStatus.COMPLETED
        assert result.transcript_status == MeetingTranscriptStatus.READY
        assert result.provider_status == "done"
        assert result.provider_payload["last_webhook_event_name"] == "bot.done"
        assert result.last_webhook_at is not None
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_video_artifact_archived_updates_stable_columns(self, monkeypatch) -> None:
        """Archived mixed-video metadata should be queryable without provider JSON."""
        organisation_id = uuid4()
        session_id = uuid4()
        event_id = uuid4()
        record = MeetingRecordingSession(
            session_id=session_id,
            organisation_id=organisation_id,
            calendar_event_id=event_id,
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://meet.example/join",
            join_at=datetime.now(UTC),
            status=MeetingRecordingStatus.COMPLETED,
            transcript_status=MeetingTranscriptStatus.READY,
            provider_session_id="bot_123",
            provider_payload={"id": "bot_123"},
        )

        service = MeetingRecordingSessionService(organisation_id)
        session, fake_gen = _fake_session_gen_with_scalar(record)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.mark_video_artifact_archived(
            session_id,
            {
                "recording_id": "rec_1",
                "video_id": "video_1",
                "archived_blob_path": "meeting_recordings/event/rec_1/video_1.mp4",
                "archived_blob_url": "https://storage.example/container/meeting_recordings/event/rec_1/video_1.mp4",
                "archived_content_type": "video/mp4",
                "archived_size_bytes": 7,
                "archived_at": "2026-07-06T01:00:00+00:00",
            },
        )

        assert result is record
        assert record.video_blob_path == "meeting_recordings/event/rec_1/video_1.mp4"
        assert (
            record.video_blob_url
            == "https://storage.example/container/meeting_recordings/event/rec_1/video_1.mp4"
        )
        assert record.video_content_type == "video/mp4"
        assert record.video_size_bytes == 7
        assert record.video_recording_id == "rec_1"
        assert record.video_artifact_id == "video_1"
        assert record.video_archived_at == datetime(2026, 7, 6, 1, 0, tzinfo=UTC)
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_by_provider_session_id_returns_matching_record(self, monkeypatch) -> None:
        """Webhook lookup resolves only the provider session id inside this service."""
        service = _make_service()
        existing = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://example/join",
            join_at=datetime.now(UTC) + timedelta(minutes=5),
            status=MeetingRecordingStatus.SENT,
            transcript_status=MeetingTranscriptStatus.PENDING,
            provider_session_id="bot_123",
            provider_payload={"id": "bot_123"},
        )
        _, fake_gen = _fake_session_gen_with_scalar(existing)
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.get_by_provider_session_id("bot_123")

        assert result is existing


class TestListUnsentUpcomingSessions:
    """Coverage for the bounded unsent-session fallback listing."""

    @pytest.mark.asyncio
    async def test_returns_unsent_upcoming_sessions(self, monkeypatch) -> None:
        """Fallback listing returns queued candidate sessions in service order."""
        service = _make_service()
        upcoming = MeetingRecordingSession(
            organisation_id=service.organisation_id,
            calendar_event_id=uuid4(),
            provider=MeetingRecordingProvider.RECALL_AI,
            dedupe_key="key",
            meeting_url="https://example/join",
            join_at=datetime.now(UTC) + timedelta(hours=1),
            status=MeetingRecordingStatus.PENDING_SEND,
            transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
        )
        session, fake_gen = _fake_session_gen_with_scalars([upcoming])
        monkeypatch.setattr(
            "src.service.meeting_bot.session_service.acquire_org_async_session",
            fake_gen,
        )

        result = await service.list_unsent_upcoming_sessions(
            before=datetime.now(UTC) + timedelta(days=1)
        )

        assert result == [upcoming]
        session.execute.assert_awaited_once()
