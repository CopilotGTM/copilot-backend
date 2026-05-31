"""Public Google Gmail and Calendar webhook entrypoint.

Google exposes two delivery mechanisms, but this API keeps one external route
so provider setup and ingress controls stay simple. The request service owns
the provider-specific parsing because Gmail Pub/Sub JSON and Calendar channel
headers have different validation contracts.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from src.service.integrations.webhooks.google.metadata import (
    GoogleSubscriptionKind,
    parse_subscription_kind,
)
from src.service.integrations.webhooks.google.request_service import (
    GOOGLE_WEBHOOK_MAX_BODY_BYTES,
    GoogleWebhookRequestService,
)

router = APIRouter(prefix="/google", tags=["integrations", "google-webhooks"])


async def get_google_webhook_request_service() -> GoogleWebhookRequestService:
    """Provide the request validator used by the public Google webhook route."""
    return GoogleWebhookRequestService()


@router.post("/webhook")
async def handle_google_webhook(
    request: Request,
    kind: str = Query(...),
    organisation_id: UUID | None = Query(default=None),
    integration_id: UUID | None = Query(default=None),
    webhook_service: GoogleWebhookRequestService = Depends(get_google_webhook_request_service),
) -> Response:
    """Dispatch one Google webhook request to the provider-specific parser.

    Gmail Pub/Sub uses a static push endpoint and resolves the integration from
    the decoded account email later. Calendar watch callbacks are per
    subscription, so their URL carries the org and integration ids.
    """
    subscription_kind = parse_subscription_kind(kind)
    if subscription_kind == GoogleSubscriptionKind.GMAIL:
        # Gmail routing data is inside the Pub/Sub payload, not the URL. Passing
        # org ids here would make a shared Pub/Sub subscription impossible.
        await webhook_service.handle_gmail_pubsub(
            request=request,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if subscription_kind == GoogleSubscriptionKind.CALENDAR:
        # Calendar channel callbacks include our generated query params, but the
        # ingestion layer still reloads the org-owned integration before syncing.
        await webhook_service.handle_calendar_channel(
            request=request,
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported Google webhook kind",
    )


__all__ = [
    "GOOGLE_WEBHOOK_MAX_BODY_BYTES",
    "get_google_webhook_request_service",
    "handle_google_webhook",
    "router",
]
