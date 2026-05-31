"""Regression tests for the public Google webhook route.

These tests lock the single-endpoint contract: Gmail uses a static Pub/Sub
route, Calendar carries route-bound ids, and unknown kinds stop before
ingestion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routers.integrations.google_webhook_routes import (
    get_google_webhook_request_service,
    router,
)


class FakeGoogleWebhookRequestService:
    """Minimal request-service double used to assert route delegation only."""

    def __init__(self) -> None:
        self.handle_gmail_pubsub = AsyncMock()
        self.handle_calendar_channel = AsyncMock()


def _client(service: FakeGoogleWebhookRequestService) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/users/integrations")

    async def _override_service() -> FakeGoogleWebhookRequestService:
        return service

    app.dependency_overrides[get_google_webhook_request_service] = _override_service
    return TestClient(app)


def test_gmail_webhook_delegates_to_request_service() -> None:
    """Gmail must not require org ids because Pub/Sub push endpoints are static."""
    service = FakeGoogleWebhookRequestService()
    client = _client(service)

    response = client.post(
        "/users/integrations/google/webhook",
        params={
            "kind": "gmail",
        },
        json={"message": {"messageId": "1", "data": "e30="}},
    )

    assert response.status_code == 204
    service.handle_gmail_pubsub.assert_awaited_once()
    assert "organisation_id" not in service.handle_gmail_pubsub.await_args.kwargs
    assert "integration_id" not in service.handle_gmail_pubsub.await_args.kwargs


def test_calendar_webhook_delegates_to_request_service() -> None:
    """Calendar must pass route-bound ids through to the request service."""
    service = FakeGoogleWebhookRequestService()
    client = _client(service)
    organisation_id = uuid4()
    integration_id = uuid4()

    response = client.post(
        "/users/integrations/google/webhook",
        params={
            "kind": "calendar",
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
        },
        headers={
            "X-Goog-Channel-ID": "channel-1",
            "X-Goog-Resource-ID": "resource-1",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Message-Number": "2",
            "X-Goog-Channel-Token": "secret",
        },
    )

    assert response.status_code == 204
    service.handle_calendar_channel.assert_awaited_once()
    assert service.handle_calendar_channel.await_args.kwargs["organisation_id"] == organisation_id
    assert service.handle_calendar_channel.await_args.kwargs["integration_id"] == integration_id


def test_webhook_rejects_unknown_kind() -> None:
    """Unsupported Google webhook kinds must not reach provider ingestion."""
    service = FakeGoogleWebhookRequestService()
    client = _client(service)

    response = client.post(
        "/users/integrations/google/webhook",
        params={
            "kind": "drive",
            "organisation_id": str(uuid4()),
            "integration_id": str(uuid4()),
        },
        json={},
    )

    assert response.status_code == 400
    service.handle_gmail_pubsub.assert_not_awaited()
    service.handle_calendar_channel.assert_not_awaited()
