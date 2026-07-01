import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.infra.config.configs import whatsapp_config


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload(message_id: str = "wamid.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "contacts": [{"wa_id": "15551234567", "profile": {"name": "Nikhil"}}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": "15551234567",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }


@pytest.fixture
def client() -> TestClient:
    from src.api.routers.bots.whatsapp_webhook_handler import router

    app = FastAPI()
    app.include_router(router, prefix="/users/bots/whatsapp")
    with TestClient(app) as test_client:
        yield test_client


def test_verify_webhook_returns_challenge_for_valid_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(whatsapp_config, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(
        whatsapp_config,
        "get_webhook_verify_token",
        AsyncMock(return_value="verify-me"),
    )

    response = client.get(
        "/users/bots/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "12345",
        },
    )

    assert response.status_code == 200
    assert response.text == "12345"


def test_verify_webhook_rejects_invalid_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(whatsapp_config, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(
        whatsapp_config,
        "get_webhook_verify_token",
        AsyncMock(return_value="verify-me"),
    )

    response = client.get(
        "/users/bots/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "12345",
        },
    )

    assert response.status_code == 403


def test_post_webhook_rejects_missing_signature(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(whatsapp_config, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(
        whatsapp_config,
        "get_app_secret",
        AsyncMock(return_value="app-secret"),
    )

    response = client.post("/users/bots/whatsapp/webhook", json=_payload())

    assert response.status_code == 401


def test_post_webhook_processes_before_returning_success(client: TestClient, monkeypatch) -> None:
    from src.api.routers.bots import whatsapp_webhook_handler

    monkeypatch.setattr(whatsapp_config, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(
        whatsapp_config,
        "get_app_secret",
        AsyncMock(return_value="app-secret"),
    )

    class FakeRedis:
        def set(self, *args, **kwargs):
            return True

    processed = []

    async def fake_process(self, payload):
        processed.append(payload)

    monkeypatch.setattr(whatsapp_webhook_handler, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        whatsapp_webhook_handler.WhatsAppBotService,
        "process_webhook",
        fake_process,
        raising=True,
    )

    body = json.dumps(_payload()).encode("utf-8")
    response = client.post(
        "/users/bots/whatsapp/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, "app-secret")},
    )

    assert response.status_code == 200
    assert processed == [_payload()]


def test_post_webhook_uses_configured_dedupe_ttl(client: TestClient, monkeypatch) -> None:
    from src.api.routers.bots import whatsapp_webhook_handler

    monkeypatch.setattr(whatsapp_config, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(whatsapp_config, "WEBHOOK_DEDUPE_TTL_SECONDS", 42)
    monkeypatch.setattr(
        whatsapp_config,
        "get_app_secret",
        AsyncMock(return_value="app-secret"),
    )

    captured: dict[str, object] = {}

    class FakeRedis:
        def set(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return True

    process_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(whatsapp_webhook_handler, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        whatsapp_webhook_handler.WhatsAppBotService,
        "process_webhook",
        process_mock,
        raising=True,
    )

    body = json.dumps(_payload()).encode("utf-8")
    response = client.post(
        "/users/bots/whatsapp/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, "app-secret")},
    )

    assert response.status_code == 200
    assert captured["kwargs"]["ex"] == 42
    assert process_mock.await_count == 1


def test_post_webhook_ignores_duplicate_message_ids(client: TestClient, monkeypatch) -> None:
    from src.api.routers.bots import whatsapp_webhook_handler

    monkeypatch.setattr(whatsapp_config, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(
        whatsapp_config,
        "get_app_secret",
        AsyncMock(return_value="app-secret"),
    )

    class FakeRedis:
        def set(self, *args, **kwargs):
            return False

    process_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(whatsapp_webhook_handler, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        whatsapp_webhook_handler.WhatsAppBotService,
        "process_webhook",
        process_mock,
        raising=True,
    )

    body = json.dumps(_payload()).encode("utf-8")
    response = client.post(
        "/users/bots/whatsapp/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, "app-secret")},
    )

    assert response.status_code == 200
    assert process_mock.await_count == 0
