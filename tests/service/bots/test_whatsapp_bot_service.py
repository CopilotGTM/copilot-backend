from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.infra.config.configs import slack_config, teams_config, whatsapp_config
from src.service.bots.whatsapp_bot_service import WhatsAppBotService, WhatsAppInboundMessage


def _message() -> WhatsAppInboundMessage:
    return WhatsAppInboundMessage(
        message_id="wamid.1",
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="hello from whatsapp",
        profile_name="Nikhil",
    )


@pytest.mark.asyncio
async def test_unlinked_user_gets_login_message(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WhatsAppBotService()
    send_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(
        service,
        "_get_login_message_with_state",
        lambda message: f"login for {message.recipient_wa_id}",
    )
    monkeypatch.setattr(service, "send_text_message", send_mock)

    await service._route_message(_message())

    send_mock.assert_awaited_once_with(
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="login for 15551234567",
    )


def test_login_message_uses_whatsapp_auth_login_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def setex(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(
        "src.service.bots.auth_state_service.get_redis_client",
        lambda: FakeRedis(),
    )
    monkeypatch.setattr(whatsapp_config, "WHATSAPP_AUTH_LOGIN_URL", "https://wa.example/bots/auth")
    monkeypatch.setattr(slack_config, "SLACK_AUTH_LOGIN_URL", "https://slack.example/bots/auth")
    monkeypatch.setattr(teams_config, "TEAMS_AUTH_LOGIN_URL", "https://teams.example/bots/auth")

    login_message = WhatsAppBotService()._get_login_message_with_state(_message())

    assert "https://wa.example/bots/auth?" in login_message
    assert "provider=whatsapp" in login_message
    assert "slack.example" not in login_message
    assert "teams.example" not in login_message


def test_login_message_does_not_fallback_to_slack_or_teams_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whatsapp_config, "WHATSAPP_AUTH_LOGIN_URL", "")
    monkeypatch.setattr(slack_config, "SLACK_AUTH_LOGIN_URL", "https://slack.example/bots/auth")
    monkeypatch.setattr(teams_config, "TEAMS_AUTH_LOGIN_URL", "https://teams.example/bots/auth")

    login_message = WhatsAppBotService()._get_login_message_with_state(_message())

    assert "Unable to generate a login link right now" in login_message
    assert "slack.example" not in login_message
    assert "teams.example" not in login_message


@pytest.mark.asyncio
async def test_linked_user_enqueues_chat_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=uuid4()))

    async def fake_send_chat_task_async(name, args, kwargs, queue):
        captured["name"] = name
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["queue"] = queue
        return "task-1"

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(_message())

    assert captured["name"] == "chat.process_bot_message"
    assert captured["args"][3] == "whatsapp"
    assert captured["args"][4] == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
    }
    assert captured["kwargs"]["bot_context"]["conversation_type"] == "personal"
    assert captured["kwargs"]["bot_context"]["external_user_id"] == "15551234567"


@pytest.mark.asyncio
async def test_status_only_payload_is_ignored() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"statuses": [{"id": "status-1"}]}}]}],
    }

    messages = service._extract_inbound_text_messages(payload)

    assert messages == []


@pytest.mark.asyncio
async def test_non_text_messages_are_ignored() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "messages": [{"id": "wamid.1", "from": "1555", "type": "image"}],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_text_messages(payload)

    assert messages == []


class _FakeAsyncSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))


@pytest.mark.asyncio
async def test_create_bot_job_creates_whatsapp_personal_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    captured_jobs = []

    class TrackingSession(_FakeAsyncSession):
        def add(self, obj):
            super().add(obj)
            if hasattr(obj, "job_type"):
                captured_jobs.append(obj)

    async def gen(_org_id):
        yield TrackingSession()

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", gen)

    job_id = await service._create_bot_job(user=user, message_text="hello")

    assert isinstance(job_id, UUID)
    assert len(captured_jobs) == 1
    assert captured_jobs[0].job_type == "whatsapp_chat_response"
    assert captured_jobs[0].chat_type == "WHATSAPP_CHAT"
