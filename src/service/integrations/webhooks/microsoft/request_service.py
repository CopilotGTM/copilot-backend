"""Request-level service for Microsoft Graph webhook callbacks.

The public route delegates here so HTTP parsing and request guardrails stay
separate from webhook ingestion business rules.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from src.infra.security.request_body import read_bounded_request_body
from src.service.integrations.webhooks.microsoft.ingestion_service import (
    MicrosoftWebhookIngestionResult,
    MicrosoftWebhookIngestionService,
)
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    parse_subscription_kind,
)

# Microsoft sends compact notification envelopes; 256 KiB leaves room for
# batching while preventing the public webhook route from becoming a body sink.
MICROSOFT_WEBHOOK_MAX_BODY_BYTES = 256 * 1024


class MicrosoftWebhookRequestService:
    """Validate Microsoft webhook HTTP requests before ingestion."""

    def __init__(self, ingestion_service: MicrosoftWebhookIngestionService | None = None) -> None:
        self._ingestion_service = ingestion_service or MicrosoftWebhookIngestionService()

    async def handle_webhook(
        self,
        *,
        request: Request,
        organisation_id: UUID | None,
        integration_id: UUID | None,
        kind: str | None,
    ) -> MicrosoftWebhookIngestionResult:
        """Validate a data-change callback and enqueue incremental sync work."""
        required_organisation_id, required_integration_id, parsed_kind = (
            self._require_routing_params(
                organisation_id=organisation_id,
                integration_id=integration_id,
                kind=kind,
            )
        )
        raw_body = await self._read_body(request)
        return await self._ingestion_service.ingest(
            organisation_id=required_organisation_id,
            integration_id=required_integration_id,
            kind=parsed_kind,
            notification_payloads=self._parse_notifications(raw_body),
        )

    async def handle_lifecycle(
        self,
        *,
        request: Request,
        organisation_id: UUID | None,
        integration_id: UUID | None,
        kind: str | None,
    ) -> MicrosoftWebhookIngestionResult:
        """Validate a lifecycle callback and enqueue immediate sync work."""
        required_organisation_id, required_integration_id, parsed_kind = (
            self._require_routing_params(
                organisation_id=organisation_id,
                integration_id=integration_id,
                kind=kind,
            )
        )
        raw_body = await self._read_body(request)
        return await self._ingestion_service.ingest_lifecycle(
            organisation_id=required_organisation_id,
            integration_id=required_integration_id,
            kind=parsed_kind,
            notification_payloads=self._parse_notifications(raw_body),
        )

    async def _read_body(self, request: Request) -> bytes:
        """Read the webhook payload while enforcing the body-size guardrail twice."""
        return await read_bounded_request_body(
            request,
            max_bytes=MICROSOFT_WEBHOOK_MAX_BODY_BYTES,
            label="Microsoft webhook",
        )

    def _parse_notifications(self, raw_body: bytes) -> list[dict[str, Any]]:
        """Parse Graph JSON and require the top-level notification list."""
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Microsoft webhook JSON payload",
            ) from exc

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Microsoft webhook notification payload",
            )

        notifications = payload.get("value")
        if not isinstance(notifications, list) or not all(
            isinstance(notification, dict) for notification in notifications
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Microsoft webhook notification payload",
            )

        return notifications

    def _parse_kind(self, kind: str) -> MicrosoftSubscriptionKind:
        """Validate and normalize the subscription kind from query params."""
        parsed_kind = parse_subscription_kind(kind)
        if parsed_kind is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Microsoft webhook subscription kind",
            )
        return parsed_kind

    def _require_routing_params(
        self,
        *,
        organisation_id: UUID | None,
        integration_id: UUID | None,
        kind: str | None,
    ) -> tuple[UUID, UUID, MicrosoftSubscriptionKind]:
        """Require query params that map webhook traffic to one org integration."""
        # Graph callbacks only include the URL we registered during subscription
        # creation; without these params we cannot enforce tenant isolation.
        if organisation_id is None or integration_id is None or kind is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Microsoft webhook routing parameters",
            )
        return organisation_id, integration_id, self._parse_kind(kind)


__all__ = [
    "MICROSOFT_WEBHOOK_MAX_BODY_BYTES",
    "MicrosoftWebhookRequestService",
]
