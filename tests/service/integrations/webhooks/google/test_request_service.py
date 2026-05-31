"""Regression tests for Google webhook request parsing.

These tests keep provider envelope details out of ingestion tests: Pub/Sub body
decoding, Calendar header parsing, and Gmail's lack of route-bound ids.
"""

import base64
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from src.service.integrations.webhooks.google.request_service import GoogleWebhookRequestService
from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)


def test_gmail_pubsub_notification_decodes_message_data() -> None:
    """Gmail Pub/Sub `message.data` must decode to account email and history id."""
    data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": "seller@example.com", "historyId": "987"}).encode()
    ).decode()

    notification = GoogleGmailPubSubNotification.from_payload(
        {"message": {"messageId": "pubsub-1", "data": data}}
    )

    assert notification is not None
    assert notification.message_id == "pubsub-1"
    assert notification.email_address == "seller@example.com"
    assert notification.history_id == "987"


def test_calendar_channel_notification_reads_required_headers() -> None:
    """Calendar channel headers must parse regardless of caller casing."""
    notification = GoogleCalendarChannelNotification.from_headers(
        {
            "x-goog-channel-id": "channel-1",
            "x-goog-resource-id": "resource-1",
            "x-goog-resource-state": "exists",
            "x-goog-message-number": "2",
            "x-goog-channel-token": "secret",
        }
    )

    assert notification is not None
    assert notification.channel_id == "channel-1"
    assert notification.resource_state == "exists"


class _FakeRequest:
    def __init__(
        self,
        body: bytes,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> None:
        self.headers = headers or {}
        self.query_params = query_params or {}
        self._body = body

    async def body(self) -> bytes:
        """Return the test body through the same awaitable API as FastAPI."""
        return self._body


@pytest.mark.asyncio
async def test_gmail_request_does_not_require_route_bound_integration_ids(monkeypatch) -> None:
    """Gmail request validation must support one static Pub/Sub push endpoint."""
    monkeypatch.setenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN", "push-secret")
    monkeypatch.setenv(
        "GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION",
        "projects/proj/subscriptions/gmail",
    )
    data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": "seller@example.com", "historyId": "987"}).encode()
    ).decode()
    ingestion_service = AsyncMock()
    request_service = GoogleWebhookRequestService(ingestion_service=ingestion_service)

    await request_service.handle_gmail_pubsub(
        request=_FakeRequest(
            json.dumps(
                {
                    "message": {"messageId": "pubsub-1", "data": data},
                    "subscription": "projects/proj/subscriptions/gmail",
                }
            ).encode(),
            query_params={"token": "push-secret"},
        ),
    )

    ingestion_service.ingest_gmail.assert_awaited_once()
    notification = ingestion_service.ingest_gmail.await_args.kwargs["notification"]
    assert notification.email_address == "seller@example.com"


@pytest.mark.asyncio
async def test_gmail_request_rejects_missing_pubsub_push_token(monkeypatch) -> None:
    """A Pub/Sub-shaped request is not trusted unless it carries our push token."""
    monkeypatch.setenv("GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN", "push-secret")
    monkeypatch.setenv(
        "GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION",
        "projects/proj/subscriptions/gmail",
    )
    data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": "seller@example.com", "historyId": "987"}).encode()
    ).decode()
    ingestion_service = AsyncMock()
    request_service = GoogleWebhookRequestService(ingestion_service=ingestion_service)

    with pytest.raises(HTTPException) as exc_info:
        await request_service.handle_gmail_pubsub(
            request=_FakeRequest(
                json.dumps(
                    {
                        "message": {"messageId": "pubsub-1", "data": data},
                        "subscription": "projects/proj/subscriptions/gmail",
                    }
                ).encode(),
            ),
        )

    assert exc_info.value.status_code == 401
    ingestion_service.ingest_gmail.assert_not_awaited()
