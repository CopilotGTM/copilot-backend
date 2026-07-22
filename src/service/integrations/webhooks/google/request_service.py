"""HTTP parsing and guardrails for Google webhook callbacks.

Called by the FastAPI route before any business validation. This module keeps
provider envelope parsing separate from ingestion so malformed public traffic
is rejected before DB lookups, durable receipts, or Celery dispatches.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import replace
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from src.infra.config.configs import google_gmail_pubsub_config
from src.infra.security.request_body import read_bounded_request_body
from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)

# Google webhook bodies should be tiny. The cap protects the public endpoint
# from oversized Pub/Sub envelopes without constraining valid provider traffic.
GOOGLE_WEBHOOK_MAX_BODY_BYTES = 256 * 1024
GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN_ENV = "GOOGLE_GMAIL_PUBSUB_PUSH_TOKEN"
GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION_ENV = "GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION"


class GoogleWebhookRequestService:
    """Validate Google webhook HTTP requests before ingestion.

    Route handlers construct this per request. Tests can inject a fake
    ingestion service to assert routing without importing worker dependencies.
    """

    def __init__(self, ingestion_service: Any | None = None) -> None:
        self._ingestion_service = ingestion_service or _build_default_ingestion_service()

    async def handle_gmail_pubsub(
        self,
        *,
        request: Request,
    ) -> Any:
        """Validate a Gmail Pub/Sub push callback and hand it to ingestion.

        Gmail Pub/Sub push subscriptions are static, so this path intentionally
        does not require `organisation_id` or `integration_id` query params.
        Ingestion resolves matching integrations from the decoded account email.
        """
        payload = self._parse_json(await self._read_body(request))
        notification = GoogleGmailPubSubNotification.from_payload(payload)
        if notification is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Gmail Pub/Sub webhook payload",
            )
        notification = self._apply_pubsub_header_metadata(
            request=request,
            notification=notification,
        )
        await self._require_gmail_pubsub_auth(request=request, notification=notification)
        return await self._ingestion_service.ingest_gmail(notification=notification)

    async def handle_calendar_channel(
        self,
        *,
        request: Request,
        organisation_id: UUID | None,
        integration_id: UUID | None,
    ) -> Any:
        """Validate a Calendar channel callback and hand it to ingestion.

        Calendar callback URLs are generated per watch request, so both route
        ids are required before parsing the provider headers.
        """
        required_organisation_id, required_integration_id = self._require_routing_params(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        notification = GoogleCalendarChannelNotification.from_headers(request.headers)
        if notification is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Google Calendar webhook headers",
            )
        return await self._ingestion_service.ingest_calendar(
            organisation_id=required_organisation_id,
            integration_id=required_integration_id,
            notification=notification,
        )

    async def _read_body(self, request: Request) -> bytes:
        """Read the request body with a public-route size guardrail."""
        return await read_bounded_request_body(
            request,
            max_bytes=GOOGLE_WEBHOOK_MAX_BODY_BYTES,
            label="Google webhook",
        )

    def _parse_json(self, raw_body: bytes) -> dict[str, Any]:
        """Parse a Google webhook JSON envelope into an object payload."""
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Google webhook JSON payload",
            ) from exc

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Google webhook JSON payload",
            )
        return payload

    def _apply_pubsub_header_metadata(
        self,
        *,
        request: Request,
        notification: GoogleGmailPubSubNotification,
    ) -> GoogleGmailPubSubNotification:
        """Use Pub/Sub no-wrapper metadata headers when the JSON body is raw Gmail data."""
        subscription = notification.subscription or request.headers.get(
            "x-goog-pubsub-subscription-name"
        )
        message_id = notification.message_id
        header_message_id = request.headers.get("x-goog-pubsub-message-id")
        if message_id.startswith("gmail:") and header_message_id:
            message_id = header_message_id
        publish_time = notification.publish_time or request.headers.get(
            "x-goog-pubsub-publish-time"
        )
        if (
            subscription == notification.subscription
            and message_id == notification.message_id
            and publish_time == notification.publish_time
        ):
            return notification
        return replace(
            notification,
            subscription=subscription,
            message_id=message_id,
            publish_time=publish_time,
        )

    def _require_routing_params(
        self,
        *,
        organisation_id: UUID | None,
        integration_id: UUID | None,
    ) -> tuple[UUID, UUID]:
        """Require Calendar callback params that bind traffic to one integration."""
        if organisation_id is None or integration_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Google webhook routing parameters",
            )
        return organisation_id, integration_id

    async def _require_gmail_pubsub_auth(
        self,
        *,
        request: Request,
        notification: GoogleGmailPubSubNotification,
    ) -> None:
        """Require the configured Pub/Sub push token and subscription name."""
        expected_token = await google_gmail_pubsub_config.get_push_token()
        expected_subscription = (
            os.environ.get(GOOGLE_GMAIL_PUBSUB_SUBSCRIPTION_ENV)
            or google_gmail_pubsub_config.SUBSCRIPTION
        )
        if not expected_token or not expected_subscription:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gmail Pub/Sub webhook authentication is not configured",
            )

        query_params = getattr(request, "query_params", {})
        provided_token = request.headers.get("x-google-gmail-pubsub-token")
        if provided_token is None:
            provided_token = query_params.get("token")
        if not isinstance(provided_token, str) or not secrets.compare_digest(
            provided_token,
            expected_token,
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Gmail Pub/Sub webhook token",
            )

        if notification.subscription != expected_subscription:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid Gmail Pub/Sub subscription",
            )


def _build_default_ingestion_service() -> Any:
    """Import ingestion lazily so route tests avoid DB and worker imports."""
    from src.service.integrations.webhooks.google.ingestion_service import (
        GoogleWebhookIngestionService,
    )

    return GoogleWebhookIngestionService()
