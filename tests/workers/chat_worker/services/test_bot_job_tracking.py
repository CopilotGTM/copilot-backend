"""Tests for bot message job creation and token tracking.

Tests cover:
- TeamsBotService._create_bot_job / SlackBotService._create_bot_job (dispatch-side)
- BotMessageProcessingService._finalize_job (worker-side token persistence)
- BotMessageProcessingService._mark_job_in_progress (status transition)
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.workers.chat_worker.services.bot_message_processing_service import (
    BotMessageProcessingService,
)

_PATCH_SESSION = "src.infra.database.sessions.org_session_scope"
_PATCH_ASYNC_SESSION = "src.infra.database.sessions.acquire_org_async_session"


@contextmanager
def _fake_session(mock_session):
    yield mock_session


@pytest.fixture
def service():
    return BotMessageProcessingService(uuid4())


# ---------------------------------------------------------------------------
# Bot service _create_bot_job tests
# ---------------------------------------------------------------------------


class _FakeAsyncSession:
    """Minimal async session mock for testing _create_bot_job."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def execute(self, stmt):
        # Return no existing session — triggers creation
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))


async def _fake_async_gen(org_id):
    yield _FakeAsyncSession()


def _make_user(org_id=None, user_id=None):
    return SimpleNamespace(
        organisation_id=org_id or uuid4(),
        user_id=user_id or uuid4(),
        email="test@example.com",
    )


@pytest.mark.asyncio
async def test_teams_create_bot_job_personal():
    """Teams personal chat: creates session + message + job."""
    from src.service.bots.teams_bot_service import TeamsBotService

    bot = object.__new__(TeamsBotService)
    user = _make_user()
    captured_jobs = []

    class TrackingSession(_FakeAsyncSession):
        def add(self, obj):
            super().add(obj)
            if hasattr(obj, "job_type"):
                captured_jobs.append(obj)

    async def gen(org_id):
        yield TrackingSession()

    with patch(_PATCH_ASYNC_SESSION, side_effect=gen):
        job_id = await bot._create_bot_job(
            user=user, message_text="hi", conversation_type="personal"
        )

    assert isinstance(job_id, UUID)
    assert len(captured_jobs) == 1
    job = captured_jobs[0]
    assert job.job_type == "teams_chat_response"
    assert job.chat_type == "TEAMS_CHAT"
    assert job.status.value == "pending"
    assert job.user_id == user.user_id


@pytest.mark.asyncio
async def test_teams_create_bot_job_group():
    """Teams group chat: creates job without message_id."""
    from src.service.bots.teams_bot_service import TeamsBotService

    bot = object.__new__(TeamsBotService)
    user = _make_user()
    captured_jobs = []

    class TrackingSession(_FakeAsyncSession):
        def add(self, obj):
            super().add(obj)
            if hasattr(obj, "job_type"):
                captured_jobs.append(obj)

    async def gen(org_id):
        yield TrackingSession()

    with patch(_PATCH_ASYNC_SESSION, side_effect=gen):
        job_id = await bot._create_bot_job(
            user=user, message_text="hi", conversation_type="groupChat"
        )

    assert isinstance(job_id, UUID)
    job = captured_jobs[0]
    assert job.message_id is None
    assert job.chat_type == "TEAMS_CHAT"


@pytest.mark.asyncio
async def test_slack_create_bot_job_channel():
    """Slack channel chat: creates job without message_id."""
    from src.service.bots.slack_bot_service import SlackBotService

    bot = object.__new__(SlackBotService)
    user = _make_user()
    captured_jobs = []

    class TrackingSession(_FakeAsyncSession):
        def add(self, obj):
            super().add(obj)
            if hasattr(obj, "job_type"):
                captured_jobs.append(obj)

    async def gen(org_id):
        yield TrackingSession()

    with patch(_PATCH_ASYNC_SESSION, side_effect=gen):
        job_id = await bot._create_bot_job(
            user=user, message_text="hi", conversation_type="channel"
        )

    assert isinstance(job_id, UUID)
    job = captured_jobs[0]
    assert job.message_id is None
    assert job.job_type == "slack_chat_response"
    assert job.chat_type == "SLACK_CHAT"


# ---------------------------------------------------------------------------
# _finalize_job (sync, worker-side)
# ---------------------------------------------------------------------------


class TestFinalizeJob:
    def test_finalize_with_token_usage(self, service):
        from src.infra.messaging.chat_stream_models import TokenUsageFrame

        job_id = uuid4()
        frames = [
            TokenUsageFrame(
                input_tokens=500,
                output_tokens=200,
                total_tokens=700,
                reasoning_tokens=0,
                cache_read_tokens=50,
                cache_write_tokens=10,
                cost=0.01,
                model_name="gpt-5.4",
                duration_seconds=2.5,
            ),
        ]

        mock_job = SimpleNamespace(
            job_id=job_id,
            status=None,
            token_usage=None,
            model_name=None,
            cost=None,
            latency_ms=None,
        )

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_job)

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            service._finalize_job(job_id, frames, success=True)

        assert mock_job.status.value == "completed"
        assert mock_job.token_usage["input_tokens"] == 500
        assert mock_job.model_name == "gpt-5.4"
        assert mock_job.cost == Decimal("0.01")
        assert mock_job.latency_ms == 2500

    def test_finalize_without_token_frames(self, service):
        job_id = uuid4()
        mock_job = SimpleNamespace(
            job_id=job_id,
            status=None,
            token_usage=None,
            model_name=None,
            cost=None,
            latency_ms=None,
        )
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_job)

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            service._finalize_job(job_id, [], success=True)

        assert mock_job.status.value == "completed"
        assert mock_job.token_usage is None

    def test_finalize_marks_failed(self, service):
        job_id = uuid4()
        mock_job = SimpleNamespace(
            job_id=job_id,
            status=None,
            token_usage=None,
            model_name=None,
            cost=None,
            latency_ms=None,
        )
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_job)

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            service._finalize_job(job_id, [], success=False)

        assert mock_job.status.value == "failed"


# ---------------------------------------------------------------------------
# _mark_job_in_progress
# ---------------------------------------------------------------------------


class TestMarkJobInProgress:
    def test_transitions_to_in_progress(self, service):
        job_id = uuid4()
        mock_job = SimpleNamespace(job_id=job_id, status=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_job)

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            service._mark_job_in_progress(job_id)

        assert mock_job.status.value == "in_progress"
