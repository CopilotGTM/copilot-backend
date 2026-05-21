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
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
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
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "deal.creation"
    assert kwargs["portal_id"] == "123"
    assert kwargs["object_id"] == "456"


def test_hubspot_webhook_queues_company_creation(monkeypatch) -> None:
    """Company creation events are accepted and queued to the integrations worker."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "company.creation",
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "company.creation"
    assert kwargs["portal_id"] == "123"
    assert kwargs["object_id"] == "456"


def test_hubspot_webhook_queues_contact_creation(monkeypatch) -> None:
    """Contact creation events are accepted and queued to the integrations worker."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "contact.creation",
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "contact.creation"
    assert kwargs["portal_id"] == "123"
    assert kwargs["object_id"] == "456"


def test_hubspot_webhook_queues_generic_note_creation(monkeypatch) -> None:
    """Generic HubSpot note creation events normalize to internal note events."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    payload = {
        "subscriptionType": "object.creation",
        "objectTypeId": "0-46",
        "portalId": 123,
        "objectId": 456,
        "eventId": 789,
    }
    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps([payload]),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    send_task.assert_awaited_once()
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "note.creation"
    assert kwargs["portal_id"] == "123"
    assert kwargs["object_id"] == "456"
    assert kwargs["event_payload"] == payload


def test_hubspot_webhook_ignores_generic_non_note_object(monkeypatch) -> None:
    """Generic object events for unsupported object families are acknowledged."""
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "object.creation",
                    "objectTypeId": "0-1",
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


def test_hubspot_webhook_ignores_unselected_note_property_change(monkeypatch) -> None:
    """Generic note property changes outside the approved list are not queued."""
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "object.propertyChange",
                    "objectTypeId": "0-46",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                    "propertyName": "hs_lastmodifieddate",
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


def test_hubspot_webhook_queues_generic_task_creation(monkeypatch) -> None:
    """Generic HubSpot task creation events normalize to internal task events."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    payload = {
        "subscriptionType": "object.creation",
        "objectTypeId": "0-27",
        "portalId": 123,
        "objectId": 456,
        "eventId": 789,
    }
    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps([payload]),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    send_task.assert_awaited_once()
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "task.creation"
    assert kwargs["portal_id"] == "123"
    assert kwargs["object_id"] == "456"
    assert kwargs["event_payload"] == payload


def test_hubspot_webhook_queues_generic_task_property_change(monkeypatch) -> None:
    """Selected generic HubSpot task property changes queue task refreshes."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    payload = {
        "subscriptionType": "object.propertyChange",
        "objectTypeId": "0-27",
        "portalId": 123,
        "objectId": 456,
        "eventId": 789,
        "propertyName": "hs_task_status",
    }
    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps([payload]),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    send_task.assert_awaited_once()
    assert send_task.await_args.kwargs["kwargs"]["subscription_type"] == "task.propertyChange"


def test_hubspot_webhook_ignores_unselected_task_property_change(monkeypatch) -> None:
    """Generic task property changes outside the approved list are not queued."""
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "object.propertyChange",
                    "objectTypeId": "0-27",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                    "propertyName": "hs_lastmodifieddate",
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


def test_hubspot_webhook_ignores_unsupported_company_event(monkeypatch) -> None:
    """Company events outside the registered v1 scope are acknowledged but not queued."""
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "company.merge",
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


def test_hubspot_webhook_ignores_unselected_company_property_change(monkeypatch) -> None:
    """Company property changes outside the approved v1 property list are not queued."""
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "company.propertyChange",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                    "propertyName": "lifecyclestage",
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


def test_hubspot_webhook_ignores_unselected_deal_property_change(monkeypatch) -> None:
    """Deal property changes outside the approved property list are not queued."""
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "deal.propertyChange",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                    "propertyName": "unmapped_custom_property",
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


def test_hubspot_webhook_coalesces_deal_creation_property_change_burst(monkeypatch) -> None:
    """A create payload with immediate deal property changes should enqueue once."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "deal.creation"
    assert kwargs["event_id"] == "222"


def test_hubspot_webhook_deal_deletion_wins_over_creation_in_same_batch(
    monkeypatch,
) -> None:
    """A same-batch deletion is the final state and should replace creation."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")
    claim = AsyncMock()
    released: list[object] = []

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        claim,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._release_refresh_pending",
        lambda event: released.append(event),
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "deal.creation",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 111,
                },
                {
                    "subscriptionType": "deal.deletion",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 222,
                },
            ]
        ),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    claim.assert_not_called()
    assert len(released) == 1
    send_task.assert_awaited_once()
    assert "countdown" not in send_task.await_args.kwargs
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "deal.deletion"
    assert kwargs["event_id"] == "222"


def test_hubspot_webhook_debounces_separate_deal_property_change_requests(
    monkeypatch,
) -> None:
    """Separate property-change requests for one deal schedule only one refresh."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")
    claimed_keys: set[str] = set()

    def claim_refresh(event) -> bool:
        """Perform the database/session operation needed by this test harness."""

        key = f"{event.subscription_type}:{event.portal_id}:{event.object_id}"
        if key in claimed_keys:
            return False
        claimed_keys.add(key)
        return True

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        claim_refresh,
    )

    for event_id, property_name in [(111, "dealname"), (222, "amount")]:
        response = client.post(
            "/users/integrations/hubspot/webhook",
            content=json.dumps(
                [
                    {
                        "subscriptionType": "deal.propertyChange",
                        "portalId": 123,
                        "objectId": 456,
                        "eventId": event_id,
                        "propertyName": property_name,
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "deal.propertyChange"
    assert kwargs["event_id"] == "111"


def test_hubspot_webhook_releases_deal_refresh_claim_when_enqueue_fails(
    monkeypatch,
) -> None:
    """Broker failures release deal refresh claims so a retry can schedule later."""
    client = _client(raise_server_exceptions=False)
    send_task = AsyncMock(side_effect=RuntimeError("broker unavailable"))
    released: list[object] = []

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._release_refresh_pending",
        lambda event: released.append(event),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._release_dedupe_claim",
        lambda *_: None,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "deal.propertyChange",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 789,
                    "propertyName": "amount",
                }
            ]
        ),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 500
    assert len(released) == 1


def test_hubspot_webhook_deal_deletion_is_not_debounced(monkeypatch) -> None:
    """Deal deletion events process immediately and clear any pending refresh."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")
    claim = AsyncMock()
    released: list[object] = []

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        claim,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._release_refresh_pending",
        lambda event: released.append(event),
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "deal.deletion",
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
    claim.assert_not_called()
    assert len(released) == 1
    send_task.assert_awaited_once()
    assert "countdown" not in send_task.await_args.kwargs
    assert send_task.await_args.kwargs["kwargs"]["subscription_type"] == "deal.deletion"


def test_hubspot_webhook_coalesces_company_creation_property_change_burst(monkeypatch) -> None:
    """A create payload with immediate company property changes should enqueue once."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "company.propertyChange",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 111,
                    "propertyName": "name",
                },
                {
                    "subscriptionType": "company.creation",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 222,
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "company.creation"
    assert kwargs["event_id"] == "222"


def test_hubspot_webhook_debounces_separate_company_property_change_requests(
    monkeypatch,
) -> None:
    """Separate property-change requests for one company schedule only one refresh."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")
    claimed_keys: set[str] = set()

    def claim_refresh(event) -> bool:
        """Perform the database/session operation needed by this test harness."""

        key = f"{event.subscription_type}:{event.portal_id}:{event.object_id}"
        if key in claimed_keys:
            return False
        claimed_keys.add(key)
        return True

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        claim_refresh,
    )

    for event_id, property_name in [(111, "name"), (222, "domain")]:
        response = client.post(
            "/users/integrations/hubspot/webhook",
            content=json.dumps(
                [
                    {
                        "subscriptionType": "company.propertyChange",
                        "portalId": 123,
                        "objectId": 456,
                        "eventId": event_id,
                        "propertyName": property_name,
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "company.propertyChange"
    assert kwargs["event_id"] == "111"


def test_hubspot_webhook_coalesces_contact_creation_property_change_burst(monkeypatch) -> None:
    """A create payload with immediate contact property changes should enqueue once."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "contact.propertyChange",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 111,
                    "propertyName": "email",
                },
                {
                    "subscriptionType": "contact.creation",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 222,
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
    assert send_task.await_args.kwargs["countdown"] == 30
    kwargs = send_task.await_args.kwargs["kwargs"]
    assert kwargs["subscription_type"] == "contact.creation"
    assert kwargs["event_id"] == "222"


def test_hubspot_webhook_does_not_coalesce_deal_and_company_events(monkeypatch) -> None:
    """Deal and company events with the same object id are queued independently."""
    client = _client()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._claim_refresh_pending",
        lambda *_: True,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "deal.creation",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 111,
                },
                {
                    "subscriptionType": "company.creation",
                    "portalId": 123,
                    "objectId": 456,
                    "eventId": 222,
                },
            ]
        ),
        headers={
            "X-HubSpot-Signature-v3": "sig",
            "X-HubSpot-Request-Timestamp": "123",
        },
    )

    assert response.status_code == 200
    assert send_task.await_count == 2
    queued_types = [
        call.kwargs["kwargs"]["subscription_type"] for call in send_task.await_args_list
    ]
    assert queued_types == ["deal.creation", "company.creation"]


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
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
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


def test_hubspot_webhook_ignores_duplicate_company_event(monkeypatch) -> None:
    """Duplicate company webhook deliveries are acknowledged without worker fanout."""
    client = _client()
    send_task = AsyncMock()

    monkeypatch.setattr(
        "src.api.routers.integrations.hubspot_webhook_routes.verify_hubspot_v3_signature",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: True,
    )

    response = client.post(
        "/users/integrations/hubspot/webhook",
        content=json.dumps(
            [
                {
                    "subscriptionType": "company.creation",
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
        "src.service.integrations.webhooks.hubspot.ingestion_service.send_integrations_task_async",
        send_task,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._is_duplicate_event",
        lambda *_: False,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.ingestion_service._release_dedupe_claim",
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
