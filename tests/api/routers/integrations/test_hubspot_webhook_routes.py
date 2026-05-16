"""Regression coverage for the public HubSpot webhook route.

These tests keep the HTTP boundary honest: signature verification happens
before queueing, unsupported/duplicate events do not reach Celery, and enqueue
failures release the dedupe claim so HubSpot can retry.
"""

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
    """Legacy contact privacy deletion events still queue through the shared route."""
    # Legacy contact privacy deletion events must keep queueing through the new
    # shared webhook registry while deal subscriptions are added beside them.
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
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


def test_hubspot_webhook_queues_deal_creation(monkeypatch) -> None:
    """Deal creation events are accepted and queued to the integrations worker."""
    # Deal creation should be treated as a first-class supported subscription
    # and leave the route as a worker task, not as an inline provider write.
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
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
                    "subscriptionType": "deal.creation",
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
    send_task.assert_awaited_once()
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "deal.creation"
    assert kwargs["portal_id"] == "123"
    assert kwargs["object_id"] == "456"


def test_hubspot_webhook_coalesces_deal_creation_property_change_burst(monkeypatch) -> None:
    """A create payload with immediate deal property changes should enqueue once."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
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
                    "subscriptionType": "deal.propertyChange",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 111,
                    "propertyName": "dealname",
                },
                {
                    "subscriptionType": "deal.creation",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 222,
                },
                {
                    "subscriptionType": "deal.propertyChange",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 333,
                    "propertyName": "dealstage",
                },
            ]
        ),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    send_task.assert_awaited_once()
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "deal.creation"
    assert kwargs["event_id"] == "222"


def test_hubspot_webhook_ignores_duplicate_event(monkeypatch) -> None:
    """Duplicate webhook deliveries are acknowledged without worker fanout."""
    # Once the route has claimed a HubSpot event, replayed deliveries should be
    # acknowledged but not queued again.
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
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
    """Broker failures release dedupe so HubSpot retries can enqueue later."""
    # If the broker write fails after dedupe claim, the claim must be released;
    # otherwise HubSpot's retry would be falsely ignored for 24 hours.
    client = _client(raise_server_exceptions=False)
    send_task = AsyncMock(side_effect=RuntimeError("broker unavailable"))
    release_calls: list[tuple[object, bytes]] = []

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
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
    """Invalid HubSpot signatures are rejected at the public route boundary."""
    # The route is public, so an invalid HubSpot v3 signature must stop the flow
    # before parsing side effects or worker fanout matter.
    client = _client()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=False),
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
    """Oversized webhook bodies are rejected before expensive route work."""
    # Oversized payloads are rejected before verifier work to bound memory and
    # CPU at the public webhook edge.
    client = _client()

    verify_signature = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        verify_signature,
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
    verify_signature.assert_not_awaited()
