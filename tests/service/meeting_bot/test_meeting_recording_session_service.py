"""Tests for MeetingRecordingSessionService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
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
