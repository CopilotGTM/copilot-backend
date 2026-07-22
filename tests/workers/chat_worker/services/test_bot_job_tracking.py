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
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.workers.chat_worker.services.bot_message_processing_service import (
    BotAgentResult,
    BotMessageProcessingService,
    WhatsAppDeliveryRetryableError,
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
async def test_slack_create_bot_job_personal_touches_session():
    """Slack personal chat activity updates the visible chat session timestamp."""
    from src.service.bots.slack_bot_service import SlackBotService

    bot = object.__new__(SlackBotService)
    user = _make_user()
    captured_jobs = []
    executed_statements = []

    class TrackingSession(_FakeAsyncSession):
        async def execute(self, stmt):
            executed_statements.append(stmt)
            return await super().execute(stmt)

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
    assert captured_jobs[0].message_id is not None
    assert any("UPDATE chat_sessions" in str(statement) for statement in executed_statements)


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
        mock_job = SimpleNamespace(
            job_id=job_id,
            job_metadata={"processing_attempts": 1},
        )
        mock_session = MagicMock()
        mock_session.execute = MagicMock(return_value=SimpleNamespace(rowcount=1))
        mock_session.get = MagicMock(return_value=mock_job)

        with patch(_PATCH_SESSION, return_value=_fake_session(mock_session)):
            should_process = service._mark_job_in_progress(job_id)

        assert should_process is True
        mock_session.commit.assert_called_once()
        assert mock_job.job_metadata["processing_stage"] == "agent_running"
        assert mock_job.job_metadata["processing_attempts"] == 2

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
        monkeypatch.setattr(service, "_job_is_completed", lambda _job_id: True)
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

    def test_process_does_not_report_stale_in_progress_job_as_success(self, monkeypatch):
        service = BotMessageProcessingService(uuid4())
        monkeypatch.setattr(service, "_mark_job_in_progress", lambda _job_id: False)
        monkeypatch.setattr(service, "_job_is_completed", lambda _job_id: False)

        result = service.process(
            user_id=uuid4(),
            message_text="hi",
            provider="whatsapp",
            reply_context={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
            bot_context={},
            job_id=uuid4(),
        )

        assert result is False


def test_whatsapp_process_persists_chunks_before_send(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.adapters import ChannelPayload

    service = BotMessageProcessingService(uuid4())
    job_id = uuid4()
    calls: list[str] = []

    monkeypatch.setattr(service, "_mark_job_in_progress", lambda _job_id: True)
    monkeypatch.setattr(
        service,
        "_prepare_bot_message_input",
        AsyncMock(return_value=("hello", {"conversation_type": "personal"})),
    )
    monkeypatch.setattr(service, "_get_session_from_job", lambda _job_id: None)
    monkeypatch.setattr(
        service,
        "_run_agent_and_accumulate",
        AsyncMock(return_value=BotAgentResult(frames=[])),
    )
    monkeypatch.setattr(
        "src.adapters.AgentResponse.from_frames",
        lambda _frames: SimpleNamespace(text="answer"),
    )
    monkeypatch.setattr(
        "src.adapters.get_adapter",
        lambda _provider: SimpleNamespace(
            render=lambda _response: ChannelPayload(type="message", content={"text": "answer"}),
            render_error=lambda text: ChannelPayload(type="message", content={"text": text}),
        ),
    )

    def persist_response(*_args, **_kwargs):
        calls.append("persist")

    def resume_delivery(_job_id):
        calls.append("send")
        return True

    monkeypatch.setattr(service, "_persist_whatsapp_response", persist_response, raising=False)
    monkeypatch.setattr(service, "resume_whatsapp_delivery", resume_delivery, raising=False)
    monkeypatch.setattr(
        service,
        "_send_payload_with_optional_override",
        AsyncMock(side_effect=AssertionError("WhatsApp must use durable send")),
    )

    result = service.process(
        user_id=uuid4(),
        message_text="hello",
        provider="whatsapp",
        reply_context={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
        bot_context={"conversation_type": "personal"},
        job_id=job_id,
    )

    assert result is True
    assert calls == ["persist", "send"]


def test_whatsapp_retryable_send_does_not_use_untracked_cleanup_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.adapters import ChannelPayload

    service = BotMessageProcessingService(uuid4())
    cleanup_mock = AsyncMock()

    monkeypatch.setattr(service, "_mark_job_in_progress", lambda _job_id: True)
    monkeypatch.setattr(
        service,
        "_prepare_bot_message_input",
        AsyncMock(return_value=("hello", {"conversation_type": "personal"})),
    )
    monkeypatch.setattr(service, "_get_session_from_job", lambda _job_id: None)
    monkeypatch.setattr(
        service,
        "_run_agent_and_accumulate",
        AsyncMock(return_value=BotAgentResult(frames=[])),
    )
    monkeypatch.setattr(
        "src.adapters.AgentResponse.from_frames",
        lambda _frames: SimpleNamespace(text="answer"),
    )
    monkeypatch.setattr(
        "src.adapters.get_adapter",
        lambda _provider: SimpleNamespace(
            render=lambda _response: ChannelPayload(type="message", content={"text": "answer"}),
            render_error=lambda text: ChannelPayload(type="message", content={"text": text}),
        ),
    )
    monkeypatch.setattr(service, "_persist_whatsapp_response", MagicMock())
    monkeypatch.setattr(
        service,
        "resume_whatsapp_delivery",
        MagicMock(side_effect=WhatsAppDeliveryRetryableError("Meta rate limited")),
    )
    monkeypatch.setattr(service, "_cleanup_thinking_indicator", cleanup_mock)

    result = service.process(
        user_id=uuid4(),
        message_text="hello",
        provider="whatsapp",
        reply_context={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
        bot_context={"conversation_type": "personal"},
        job_id=uuid4(),
    )

    assert result is False
    cleanup_mock.assert_not_awaited()


def test_whatsapp_send_only_recovery_skips_accepted_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.service.bots import whatsapp_bot_service

    service = BotMessageProcessingService(uuid4())
    job_id = uuid4()
    sent_texts: list[str] = []
    lease = {
        "reply_context": {
            "phone_number_id": "phone-123",
            "recipient_wa_id": "15551234567",
        },
        "chunks": [
            {"index": 0, "text": "already sent", "provider_message_id": "wamid.first"},
            {"index": 1, "text": "send me", "provider_message_id": None},
        ],
        "attempt": 2,
        "agent_success": True,
    }

    class FakeWhatsAppBotService:
        async def send_text_message_result(self, **kwargs):
            sent_texts.append(kwargs["text"])
            return SimpleNamespace(
                accepted=True,
                provider_message_id="wamid.second",
                failure_kind=None,
                status_code=200,
                error_message=None,
            )

    monkeypatch.setattr(service, "_lease_whatsapp_delivery", lambda _job_id: lease, raising=False)
    accepted_mock = MagicMock()
    monkeypatch.setattr(
        service,
        "_record_whatsapp_chunk_accepted",
        accepted_mock,
        raising=False,
    )
    complete_mock = MagicMock()
    monkeypatch.setattr(service, "_complete_whatsapp_delivery", complete_mock, raising=False)
    monkeypatch.setattr(
        service,
        "_run_agent_and_accumulate",
        AsyncMock(side_effect=AssertionError("send-only recovery must not run the agent")),
    )
    monkeypatch.setattr(
        whatsapp_bot_service,
        "WhatsAppBotService",
        FakeWhatsAppBotService,
    )

    assert service.resume_whatsapp_delivery(job_id) is True
    assert sent_texts == ["send me"]
    accepted_mock.assert_called_once()
    complete_mock.assert_called_once_with(job_id)


def test_accepted_whatsapp_chunk_renews_send_lease(service) -> None:
    job_id = uuid4()
    assistant_message_id = uuid4()
    old_lease = "2026-07-10T00:00:00+00:00"
    job = SimpleNamespace(
        job_metadata={
            "send_lease_at": old_lease,
            "assistant_message_id": str(assistant_message_id),
            "outbound_chunks": [{"index": 0, "text": "answer", "provider_message_id": None}],
        }
    )
    assistant_message = SimpleNamespace(extra={})
    session = MagicMock()
    session.get.side_effect = [job, assistant_message]

    with patch(_PATCH_SESSION, return_value=_fake_session(session)):
        service._record_whatsapp_chunk_accepted(
            job_id,
            0,
            SimpleNamespace(provider_message_id="wamid.accepted", status_code=200),
        )

    assert job.job_metadata["outbound_chunks"][0]["provider_message_id"] == "wamid.accepted"
    assert job.job_metadata["send_lease_at"] != old_lease
    # Replies to any outbound chunk resolve to the one persisted assistant turn.
    assert assistant_message.extra["whatsapp_message_ids"] == ["wamid.accepted"]


def test_whatsapp_response_links_assistant_clarification_to_pending_action(service) -> None:
    """Reply to an assistant clarification must resolve the same pending action later."""
    job_id = uuid4()
    session_id = uuid4()
    job = SimpleNamespace(
        message_id=uuid4(),
        job_metadata={"session_id": str(session_id), "processing_stage": "agent_running"},
        status=None,
        error_message=None,
        token_usage=None,
        model_name=None,
        cost=None,
        latency_ms=None,
    )
    session = MagicMock()
    session.get.return_value = job

    with patch(_PATCH_SESSION, return_value=_fake_session(session)):
        service._persist_whatsapp_response(
            job_id,
            text="Which deal should I use?",
            assistant_text="Which deal should I use?",
            frames=[],
            agent_success=True,
            error_message=None,
            pending_action_id="action-1",
        )

    assistant_message = session.add.call_args_list[0].args[0]
    assert assistant_message.extra["pending_action_id"] == "action-1"


def test_terminal_whatsapp_send_failure_marks_job_failed(service) -> None:
    from src.domain.models.organisation_tables.job_models import JobStatus

    job_id = uuid4()
    job = SimpleNamespace(
        status=JobStatus.IN_PROGRESS,
        error_message=None,
        job_metadata={
            "agent_success": True,
            "send_attempts": 1,
            "send_status": "sending",
            "processing_stage": "sending",
        },
    )
    session = MagicMock()
    session.get.return_value = job

    with patch(_PATCH_SESSION, return_value=_fake_session(session)):
        service._record_whatsapp_send_failure(
            job_id,
            SimpleNamespace(
                failure_kind="terminal",
                status_code=401,
                error_message="invalid access token",
            ),
        )

    assert job.status == JobStatus.FAILED
    assert job.job_metadata["send_status"] == "terminal"
    assert job.job_metadata["failure_stage"] == "sending"
    assert job.error_message == "invalid access token"


def test_retryable_whatsapp_send_failure_remains_recoverable(service) -> None:
    from src.domain.models.organisation_tables.job_models import JobStatus

    job_id = uuid4()
    job = SimpleNamespace(
        status=JobStatus.IN_PROGRESS,
        error_message=None,
        job_metadata={
            "agent_success": True,
            "send_attempts": 1,
            "send_status": "sending",
            "processing_stage": "sending",
        },
    )
    session = MagicMock()
    session.get.return_value = job

    with patch(_PATCH_SESSION, return_value=_fake_session(session)):
        service._record_whatsapp_send_failure(
            job_id,
            SimpleNamespace(
                failure_kind="retryable",
                status_code=429,
                error_message="rate limited",
            ),
        )

    assert job.status == JobStatus.IN_PROGRESS
    assert job.job_metadata["send_status"] == "retryable"
    assert job.job_metadata["processing_stage"] == "response_ready"
    assert job.job_metadata["next_send_attempt_at"] is not None


def test_whatsapp_exception_before_agent_result_persists_static_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.adapters import ChannelPayload

    service = BotMessageProcessingService(uuid4())
    job_id = uuid4()
    persist_mock = MagicMock()

    monkeypatch.setattr(service, "_mark_job_in_progress", lambda _job_id: True)
    monkeypatch.setattr(
        service,
        "_prepare_bot_message_input",
        AsyncMock(side_effect=RuntimeError("transcription failed")),
    )
    monkeypatch.setattr(
        "src.adapters.get_adapter",
        lambda _provider: SimpleNamespace(
            render_error=lambda text: ChannelPayload(type="message", content={"text": text})
        ),
    )
    monkeypatch.setattr(service, "_persist_whatsapp_response", persist_mock)
    monkeypatch.setattr(service, "resume_whatsapp_delivery", lambda _job_id: True)

    result = service.process(
        user_id=uuid4(),
        message_text="",
        provider="whatsapp",
        reply_context={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
        bot_context={"conversation_type": "personal"},
        job_id=job_id,
    )

    assert result is False
    assert persist_mock.call_args.kwargs["agent_success"] is False
    assert persist_mock.call_args.kwargs["only_if_agent_running"] is True
    assert "transcription failed" in persist_mock.call_args.kwargs["error_message"]
