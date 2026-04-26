import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.infra.config import configs
from src.infra.config.configs import google_chat_config
from src.service.bots.google_chat_bot_service import GoogleChatBotService, GoogleChatInboundEvent


def _event() -> GoogleChatInboundEvent:
    return GoogleChatInboundEvent(
        event_type="MESSAGE",
        event_id="evt-1",
        message_name="spaces/AAA/messages/BBB",
        space_name="spaces/AAA",
        thread_name="spaces/AAA/threads/THREAD1",
        external_user_id="users/123",
        external_tenant_id="spaces/AAA",
        text="hello kai",
        display_name="Nikhil",
        email="nikhil@example.com",
        timezone="Asia/Calcutta",
    )


def test_extract_message_event_normalizes_google_chat_payload() -> None:
    service = GoogleChatBotService()

    event = service._extract_event(
        {
            "type": "MESSAGE",
            "eventId": "evt-1",
            "message": {
                "name": "spaces/AAA/messages/BBB",
                "text": " hello kai ",
                "thread": {"name": "spaces/AAA/threads/THREAD1"},
            },
            "space": {"name": "spaces/AAA", "type": "DM"},
            "user": {
                "name": "users/123",
                "displayName": "Nikhil",
                "email": "nikhil@example.com",
                "timeZone": "Asia/Calcutta",
            },
        }
    )

    assert event == _event()


def test_login_message_uses_google_chat_auth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeStateService:
        def create_state(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(state_token="state-1", provider=kwargs["provider"])

        def generate_login_url(self, _state):
            return "https://app.example/bots/auth?state=state-1&provider=google_chat"

    service = GoogleChatBotService()
    monkeypatch.setattr(google_chat_config, "GOOGLE_CHAT_AUTH_LOGIN_URL", "https://app.example")
    monkeypatch.setattr(service, "auth_state_service", FakeStateService())

    message = service._get_login_message_with_state(_event())

    assert "https://app.example/bots/auth" in message["text"]
    assert captured["provider"] == ExternalIdentityProvider.GOOGLE_CHAT
    assert captured["external_user_id"] == "users/123"
    assert captured["external_tenant_id"] == "spaces/AAA"
    assert captured["context"] == {
        "space_name": "spaces/AAA",
        "thread_name": "spaces/AAA/threads/THREAD1",
    }


def test_google_chat_config_uses_adc_credentials_without_key_vault_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake_credentials = object()

    def fake_default(*, scopes):
        captured["scopes"] = scopes
        return fake_credentials, "copilotgtm-local"

    monkeypatch.setattr(configs.google.auth, "default", fake_default)

    config = configs.GoogleChatConfig()
    credentials = config.get_app_credentials()

    assert credentials is fake_credentials
    assert captured["scopes"] == ["https://www.googleapis.com/auth/chat.bot"]
    assert not hasattr(config, "SECRET_NAME_SERVICE_ACCOUNT_CREDENTIALS")
    assert not hasattr(config, "get_service_account_credentials_json")


def test_google_chat_config_allows_explicit_audience_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GOOGLE_CHAT_AUTH_AUDIENCE",
        "https://ngrok.example/users/bots/google-chat/events",
    )

    reloaded_configs = importlib.reload(configs)

    assert (
        reloaded_configs.GoogleChatConfig.GOOGLE_CHAT_AUTH_AUDIENCE
        == "https://ngrok.example/users/bots/google-chat/events"
    )

    monkeypatch.delenv("GOOGLE_CHAT_AUTH_AUDIENCE")
    importlib.reload(configs)


@pytest.mark.asyncio
async def test_get_app_access_token_refreshes_adc_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GoogleChatBotService()
    refreshed: dict[str, bool] = {}

    class FakeCredentials:
        token = "adc-token"

        def refresh(self, _request):
            refreshed["called"] = True

    monkeypatch.setattr(
        google_chat_config,
        "get_app_credentials",
        lambda: FakeCredentials(),
    )

    token = await service._get_app_access_token()

    assert token == "adc-token"
    assert refreshed == {"called": True}


@pytest.mark.asyncio
async def test_unlinked_message_returns_login_response(monkeypatch: pytest.MonkeyPatch) -> None:
    service = GoogleChatBotService()

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(
        service,
        "_get_login_message_with_state",
        lambda event: {"text": f"login for {event.external_user_id}"},
    )

    response = await service.process_event(
        {
            "type": "MESSAGE",
            "eventId": "evt-1",
            "message": {"name": "spaces/AAA/messages/BBB", "text": "hello"},
            "space": {"name": "spaces/AAA"},
            "user": {"name": "users/123"},
        }
    )

    assert response == {"text": "login for users/123"}


@pytest.mark.asyncio
async def test_linked_message_creates_job_and_enqueues_chat_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GoogleChatBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    captured: dict[str, object] = {}
    job_id = uuid4()

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=job_id))

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

    response = await service._route_message(_event())

    assert response == {"text": "Thinking..."}
    assert captured["name"] == "chat.process_bot_message"
    assert captured["args"][2] == "hello kai"
    assert captured["args"][3] == "google_chat"
    assert captured["args"][4] == {
        "space_name": "spaces/AAA",
        "thread_name": "spaces/AAA/threads/THREAD1",
    }
    assert captured["kwargs"]["bot_context"]["conversation_type"] == "personal"
    assert captured["kwargs"]["bot_context"]["external_user_id"] == "users/123"
    assert captured["kwargs"]["bot_context"]["user_timezone"] == "Asia/Calcutta"
    assert captured["kwargs"]["job_id"] == str(job_id)


@pytest.mark.asyncio
async def test_create_bot_job_creates_google_chat_personal_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GoogleChatBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    captured_jobs = []

    class FakeAsyncSession:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)
            if hasattr(obj, "job_type"):
                captured_jobs.append(obj)

        async def commit(self):
            pass

        async def flush(self):
            pass

        async def execute(self, _stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))

    async def gen(_org_id):
        yield FakeAsyncSession()

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", gen)

    job_id = await service._create_bot_job(user=user, message_text="hello")

    assert isinstance(job_id, UUID)
    assert len(captured_jobs) == 1
    assert captured_jobs[0].job_type == "google_chat_response"
    assert captured_jobs[0].chat_type == "GOOGLE_CHAT"
