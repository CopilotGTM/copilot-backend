from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.integrations.microsoft_webhook_routes import (
    get_microsoft_webhook_request_service,
    router,
)
from src.service.integrations.webhooks.microsoft.metadata import MicrosoftSubscriptionKind


class FakeMicrosoftWebhookRequestService:
    def __init__(self) -> None:
        self.handle_webhook = AsyncMock()
        self.handle_lifecycle = AsyncMock()


def _client(service: FakeMicrosoftWebhookRequestService | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/users/integrations")

    if service is not None:

        async def _override_service() -> FakeMicrosoftWebhookRequestService:
            return service

        app.dependency_overrides[get_microsoft_webhook_request_service] = _override_service
    return TestClient(app)


def test_microsoft_webhook_validation_token_returns_plain_text() -> None:
    service = FakeMicrosoftWebhookRequestService()
    client = _client(service)

    response = client.post("/users/integrations/microsoft/webhook?validationToken=abc%20123")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "abc 123"
    service.handle_webhook.assert_not_awaited()


def test_microsoft_webhook_validation_token_is_not_double_decoded() -> None:
    service = FakeMicrosoftWebhookRequestService()
    client = _client(service)

    response = client.post("/users/integrations/microsoft/webhook?validationToken=abc%2520def")

    assert response.status_code == 200
    assert response.text == "abc%20def"
    service.handle_webhook.assert_not_awaited()


def test_microsoft_webhook_calls_request_service_and_returns_accepted() -> None:
    service = FakeMicrosoftWebhookRequestService()
    client = _client(service)
    organisation_id = uuid4()
    integration_id = uuid4()
    notification = {
        "subscriptionId": "sub-email",
        "clientState": "state",
        "changeType": "created",
        "resource": "/me/messages/message-1",
    }

    response = client.post(
        "/users/integrations/microsoft/webhook",
        params={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "kind": "email",
        },
        json={"value": [notification]},
    )

    assert response.status_code == 202
    service.handle_webhook.assert_awaited_once()
    call_kwargs = service.handle_webhook.await_args.kwargs
    assert call_kwargs["organisation_id"] == organisation_id
    assert call_kwargs["integration_id"] == integration_id
    assert call_kwargs["kind"] == MicrosoftSubscriptionKind.EMAIL.value
    assert call_kwargs["request"].url.path == "/users/integrations/microsoft/webhook"


def test_microsoft_webhook_rejects_invalid_utf8_body() -> None:
    client = _client()

    response = client.post(
        "/users/integrations/microsoft/webhook",
        params={
            "organisation_id": str(uuid4()),
            "integration_id": str(uuid4()),
            "kind": "email",
        },
        content=b"\xff",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Microsoft webhook JSON payload"


def test_microsoft_webhook_lifecycle_calls_request_service_and_returns_accepted() -> None:
    service = FakeMicrosoftWebhookRequestService()
    client = _client(service)
    organisation_id = uuid4()
    integration_id = uuid4()
    notification = {
        "subscriptionId": "sub-email",
        "clientState": "state",
        "changeType": "missed",
        "resource": "/me/messages",
    }

    response = client.post(
        "/users/integrations/microsoft/webhook/lifecycle",
        params={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "kind": "email",
        },
        json={"value": [notification]},
    )

    assert response.status_code == 202
    service.handle_lifecycle.assert_awaited_once()
    call_kwargs = service.handle_lifecycle.await_args.kwargs
    assert call_kwargs["organisation_id"] == organisation_id
    assert call_kwargs["integration_id"] == integration_id
    assert call_kwargs["kind"] == MicrosoftSubscriptionKind.EMAIL.value
    assert call_kwargs["request"].url.path == "/users/integrations/microsoft/webhook/lifecycle"
