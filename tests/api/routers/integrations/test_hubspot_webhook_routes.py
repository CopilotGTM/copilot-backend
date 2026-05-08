from __future__ import annotations

import json
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.integrations.hubspot_webhook_routes import (
    HUBSPOT_WEBHOOK_MAX_BODY_BYTES,
    router,
)
from src.service.integrations.webhooks.hubspot.constants import HUBSPOT_WEBHOOK_EVENT_TASK


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/users/integrations")
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_hubspot_webhook_handles_contact_privacy_deletion(monkeypatch) -> None:
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("client-id", "client-secret")),
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        lambda **_: True,
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes._is_duplicate_event",
        lambda *_: False,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "contact.privacyDeletion",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                }
            ]
        ),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    send_task.assert_awaited_once_with(
        task_name=HUBSPOT_WEBHOOK_EVENT_TASK,
        kwargs={
            "subscription_type": "contact.privacyDeletion",
            "portal_id": "123",
            "object_id": "456",
            "event_id": "789",
            "occurred_at": None,
            "event_payload": {
                "subscriptionType": "contact.privacyDeletion",
                "portalId": 123,
                "objectId": 456,
                "eventId": 789,
            },
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
    )


def test_hubspot_webhook_ignores_duplicate_event(monkeypatch) -> None:
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("client-id", "client-secret")),
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        lambda **_: True,
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes._is_duplicate_event",
        lambda *_: True,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "contact.privacyDeletion",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                }
            ]
        ),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    send_task.assert_not_awaited()


def test_hubspot_webhook_releases_dedupe_claim_when_enqueue_fails(monkeypatch) -> None:
    client = _client(raise_server_exceptions=False)
    send_task = AsyncMock(side_effect=RuntimeError("broker unavailable"))
    release_calls: list[tuple[object, bytes]] = []

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("client-id", "client-secret")),
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        lambda **_: True,
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes._release_dedupe_claim",
        lambda event, raw_body: release_calls.append((event, raw_body)),
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "contact.privacyDeletion",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                }
            ]
        ),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 500
    assert len(release_calls) == 1


def test_hubspot_webhook_rejects_invalid_signature(monkeypatch) -> None:
    client = _client()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("client-id", "client-secret")),
    )
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        lambda **_: False,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content="[]",
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 401


def test_hubspot_webhook_rejects_oversized_body(monkeypatch) -> None:
    client = _client()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("client-id", "client-secret")),
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=b"x" * (HUBSPOT_WEBHOOK_MAX_BODY_BYTES + 1),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 413
