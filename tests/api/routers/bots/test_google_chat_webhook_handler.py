from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.service.bots.auth_validators.base import AuthValidationResult


def _message_payload(
    event_id: str = "evt-1", message_name: str = "spaces/AAA/messages/BBB"
) -> dict:
    return {
        "type": "MESSAGE",
        "eventTime": "2026-04-26T10:00:00Z",
        "message": {
            "name": message_name,
            "text": "hello kai",
            "thread": {"name": "spaces/AAA/threads/THREAD1"},
        },
        "space": {"name": "spaces/AAA", "type": "DM"},
        "user": {
            "name": "users/123",
            "displayName": "Nikhil",
            "email": "nikhil@example.com",
            "timeZone": "Asia/Calcutta",
        },
        "eventId": event_id,
    }


def _client() -> TestClient:
    from src.api.routers.bots.google_chat_webhook_handler import router

    app = FastAPI()
    app.include_router(router, prefix="/users/bots/google-chat")
    return TestClient(app)


def test_google_chat_events_rejects_missing_authorization(monkeypatch) -> None:
    from src.api.routers.bots import google_chat_webhook_handler

    monkeypatch.setattr(google_chat_webhook_handler.google_chat_config, "GOOGLE_CHAT_ENABLED", True)

    with _client() as client:
        response = client.post("/users/bots/google-chat/events", json=_message_payload())

    assert response.status_code == 401


def test_google_chat_events_returns_welcome_for_added_to_space(monkeypatch) -> None:
    from src.api.routers.bots import google_chat_webhook_handler

    payload = {"type": "ADDED_TO_SPACE", "space": {"name": "spaces/AAA"}}
    monkeypatch.setattr(google_chat_webhook_handler.google_chat_config, "GOOGLE_CHAT_ENABLED", True)
    monkeypatch.setattr(
        google_chat_webhook_handler.GoogleChatAuthValidator,
        "validate",
        AsyncMock(return_value=AuthValidationResult(True)),
    )

    with _client() as client:
        response = client.post(
            "/users/bots/google-chat/events",
            json=payload,
            headers={"Authorization": "Bearer valid-token"},
        )

    assert response.status_code == 200
    assert "Kai" in response.json()["text"]


def test_google_chat_auth_uses_configured_audience(monkeypatch) -> None:
    from src.api.routers.bots import google_chat_webhook_handler

    captured: dict[str, str] = {}
    monkeypatch.setattr(google_chat_webhook_handler.google_chat_config, "GOOGLE_CHAT_ENABLED", True)
    monkeypatch.setattr(
        google_chat_webhook_handler.google_chat_config,
        "GOOGLE_CHAT_AUTH_AUDIENCE",
        "https://api.example/users/bots/google-chat/events",
    )

    async def fake_validate(self, _headers, _body=None):
        captured["audience"] = self._audience
        return AuthValidationResult(True)

    monkeypatch.setattr(
        google_chat_webhook_handler.GoogleChatAuthValidator,
        "validate",
        fake_validate,
    )

    with _client() as client:
        response = client.post(
            "/users/bots/google-chat/events",
            json={"type": "ADDED_TO_SPACE", "space": {"name": "spaces/AAA"}},
            headers={"Authorization": "Bearer valid-token"},
        )

    assert response.status_code == 200
    assert captured["audience"] == "https://api.example/users/bots/google-chat/events"


def test_google_chat_events_dedupes_duplicate_message_events(monkeypatch) -> None:
    from src.api.routers.bots import google_chat_webhook_handler

    monkeypatch.setattr(google_chat_webhook_handler.google_chat_config, "GOOGLE_CHAT_ENABLED", True)
    monkeypatch.setattr(
        google_chat_webhook_handler.GoogleChatAuthValidator,
        "validate",
        AsyncMock(return_value=AuthValidationResult(True)),
    )

    class FakeRedis:
        def set(self, *args, **kwargs):
            return False

    process_mock = AsyncMock(return_value={"text": "Thinking..."})
    monkeypatch.setattr(google_chat_webhook_handler, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        google_chat_webhook_handler.GoogleChatBotService,
        "process_event",
        process_mock,
        raising=True,
    )

    with _client() as client:
        response = client.post(
            "/users/bots/google-chat/events",
            json=_message_payload(),
            headers={"Authorization": "Bearer valid-token"},
        )

    assert response.status_code == 200
    assert response.json() == {}
    assert process_mock.await_count == 0


def test_google_chat_events_returns_service_response_for_message(monkeypatch) -> None:
    from src.api.routers.bots import google_chat_webhook_handler

    monkeypatch.setattr(google_chat_webhook_handler.google_chat_config, "GOOGLE_CHAT_ENABLED", True)
    monkeypatch.setattr(
        google_chat_webhook_handler.GoogleChatAuthValidator,
        "validate",
        AsyncMock(return_value=AuthValidationResult(True)),
    )

    class FakeRedis:
        def set(self, *args, **kwargs):
            return True

    process_mock = AsyncMock(return_value={"text": "Thinking..."})
    monkeypatch.setattr(google_chat_webhook_handler, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        google_chat_webhook_handler.GoogleChatBotService,
        "process_event",
        process_mock,
        raising=True,
    )

    with _client() as client:
        response = client.post(
            "/users/bots/google-chat/events",
            json=_message_payload(),
            headers={"Authorization": "Bearer valid-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"text": "Thinking..."}
    process_mock.assert_awaited_once()
