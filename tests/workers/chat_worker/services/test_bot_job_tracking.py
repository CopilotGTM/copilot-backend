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
    BotAgentResult,
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


@pytest.mark.asyncio
async def test_whatsapp_create_bot_job_personal():
    """WhatsApp personal chat: creates session + message + job."""
    from src.service.bots.whatsapp_bot_service import WhatsAppBotService

    bot = WhatsAppBotService()
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
        job_id = await bot._create_bot_job(user=user, message_text="hi")

    assert isinstance(job_id, UUID)
    assert len(captured_jobs) == 1
    job = captured_jobs[0]
    assert job.job_type == "whatsapp_chat_response"
    assert job.chat_type == "WHATSAPP_CHAT"
    assert job.status.value == "pending"
    assert job.user_id == user.user_id


@pytest.mark.asyncio
async def test_send_whatsapp_payload_uses_whatsapp_bot_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.adapters import ChannelPayload

    captured: dict[str, str] = {}

    class FakeWhatsAppBotService:
        async def send_text_message(
            self, phone_number_id: str, recipient_wa_id: str, text: str
        ) -> bool:
            captured["phone_number_id"] = phone_number_id
            captured["recipient_wa_id"] = recipient_wa_id
            captured["text"] = text
            return True

    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.WhatsAppBotService",
        FakeWhatsAppBotService,
    )

    service = BotMessageProcessingService(uuid4())
    sent = await service._send_payload(
        "whatsapp",
        {"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
        ChannelPayload(type="message", content={"text": "hello"}),
    )

    assert sent is True
    assert captured == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
        "text": "hello",
    }


@pytest.mark.asyncio
async def test_send_whatsapp_payload_splits_long_text_on_paragraphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.adapters import ChannelPayload

    sent_texts: list[str] = []

    class FakeWhatsAppBotService:
        async def send_text_message(
            self, phone_number_id: str, recipient_wa_id: str, text: str
        ) -> bool:
            sent_texts.append(text)
            return True

    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.WhatsAppBotService",
        FakeWhatsAppBotService,
    )

    first = "A" * 2500
    second = "B" * 2500
    service = BotMessageProcessingService(uuid4())
    sent = await service._send_payload(
        "whatsapp",
        {"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
        ChannelPayload(type="message", content={"text": f"{first}\n\n{second}"}),
    )

    assert sent is True
    assert sent_texts == [first, second]


@pytest.mark.asyncio
async def test_send_whatsapp_payload_fails_if_any_chunk_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.adapters import ChannelPayload

    sent_texts: list[str] = []

    class FakeWhatsAppBotService:
        async def send_text_message(
            self, phone_number_id: str, recipient_wa_id: str, text: str
        ) -> bool:
            sent_texts.append(text)
            return len(sent_texts) == 1

    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.WhatsAppBotService",
        FakeWhatsAppBotService,
    )

    service = BotMessageProcessingService(uuid4())
    sent = await service._send_payload(
        "whatsapp",
        {"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
        ChannelPayload(type="message", content={"text": f"{'A' * 2500}\n\n{'B' * 2500}"}),
    )

    assert sent is False
    assert len(sent_texts) == 2


@pytest.mark.asyncio
async def test_send_whatsapp_payload_requires_reply_context() -> None:
    from src.adapters import ChannelPayload

    service = BotMessageProcessingService(uuid4())
    sent = await service._send_payload("whatsapp", {}, ChannelPayload(type="message", content={}))

    assert sent is False


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
        mock_session = MagicMock()
        mock_session.execute = MagicMock(return_value=SimpleNamespace(rowcount=1))

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            should_process = service._mark_job_in_progress(job_id)

        assert should_process is True
        mock_session.commit.assert_called_once()
        mock_session.get.assert_not_called()

    def test_completed_job_is_not_reopened(self, service):
        from src.domain.models.organisation_tables.job_models import JobStatus

        job_id = uuid4()
        mock_job = SimpleNamespace(job_id=job_id, status=JobStatus.COMPLETED)
        mock_session = MagicMock()
        mock_session.execute = MagicMock(return_value=SimpleNamespace(rowcount=0))
        mock_session.get = MagicMock(return_value=mock_job)

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            should_process = service._mark_job_in_progress(job_id)

        assert should_process is False
        assert mock_job.status == JobStatus.COMPLETED

    def test_in_progress_job_is_not_reopened(self, service):
        from src.domain.models.organisation_tables.job_models import JobStatus

        job_id = uuid4()
        mock_job = SimpleNamespace(job_id=job_id, status=JobStatus.IN_PROGRESS)
        mock_session = MagicMock()
        mock_session.execute = MagicMock(return_value=SimpleNamespace(rowcount=0))
        mock_session.get = MagicMock(return_value=mock_job)

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            should_process = service._mark_job_in_progress(job_id)

        assert should_process is False
        assert mock_job.status == JobStatus.IN_PROGRESS

    def test_process_skips_completed_job_without_agent_or_send(self, monkeypatch):
        service = BotMessageProcessingService(uuid4())
        job_id = uuid4()
        calls: list[str] = []

        async def fake_prepare_bot_message_input(**_kwargs):
            calls.append("prepare")
            return "hi", {}

        async def fake_run_agent_and_accumulate(*_args, **_kwargs):
            calls.append("agent")
            return BotAgentResult()

        async def fake_send_payload(*_args, **_kwargs):
            calls.append("send")
            return True

        monkeypatch.setattr(service, "_mark_job_in_progress", lambda _job_id: False)
        monkeypatch.setattr(service, "_prepare_bot_message_input", fake_prepare_bot_message_input)
        monkeypatch.setattr(service, "_run_agent_and_accumulate", fake_run_agent_and_accumulate)
        monkeypatch.setattr(service, "_send_payload", fake_send_payload)

        result = service.process(
            user_id=uuid4(),
            message_text="hi",
            provider="whatsapp",
            reply_context={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
            bot_context={},
            job_id=job_id,
        )

        assert result is True
        assert calls == []
