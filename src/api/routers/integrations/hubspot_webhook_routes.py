"""Public HubSpot webhook endpoint.

HubSpot calls this unauthenticated route directly, so the route owns signature
verification and event-shape validation. HubSpot-specific filtering, dedupe,
debounce, coalescing, and fanout live in the webhook ingestion service.

This route intentionally returns only an HTTP acknowledgement to HubSpot. Any
operational detail about queued, duplicate, or ignored events belongs in logs
and metrics, not in the public provider-facing response body.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.hubspot.ingestion_service import (
    HubSpotWebhookIngestionService,
)
from src.service.integrations.webhooks.hubspot.verifier import (
    HubSpotWebhookVerificationError,
    verify_hubspot_v3_signature,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/hubspot", tags=["integrations", "hubspot-webhooks"])

# Bound the public unauthenticated edge before signature verification work.
# HubSpot webhook batches are small, so 256 KiB leaves headroom without letting
# malformed requests force large body reads into API memory.
HUBSPOT_WEBHOOK_MAX_BODY_BYTES = 256 * 1024


async def get_hubspot_webhook_ingestion_service() -> HubSpotWebhookIngestionService:
    """Create the service dependency for HubSpot webhook ingestion.

    The service is stateless today, but keeping it behind FastAPI dependency
    injection matches the rest of the API layer and gives tests or internal
    diagnostics a clean override point.
    """
    return HubSpotWebhookIngestionService()


def _parse_events(raw_body: bytes) -> list[dict[str, Any]]:
    """Normalize HubSpot's single-event or batch payload into a list.

    HubSpot may deliver either shape depending on subscription and batching
    behavior. Downstream ingestion only deals with a list so the filtering and
    coalescing rules stay in one place.
    """
    try:
        payload = json.loads(raw_body.decode("utf-8") or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid HubSpot webhook JSON payload",
        ) from exc

    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and all(isinstance(event, dict) for event in payload):
        return payload

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid HubSpot webhook event payload",
    )


@router.post("/webhook")
async def handle_hubspot_webhook(
    request: Request,
    ingestion_service: HubSpotWebhookIngestionService = Depends(
        get_hubspot_webhook_ingestion_service
    ),
) -> Response:
    """Validate and enqueue HubSpot webhook events.

    Called directly by HubSpot, without user authentication. The route rejects
    invalid public traffic, then hands the verified payload to the ingestion
    service so event-specific rules stay outside the HTTP boundary.
    """
    # Prefer the declared size check when present so oversized requests are
    # rejected before reading the body. The actual byte-length check below is
    # still required because public clients can omit or lie about this header.
    content_length = request.headers.get("content-length")
    try:
        declared_content_length = int(content_length) if content_length else 0
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Content-Length header",
        ) from exc

    if declared_content_length > HUBSPOT_WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="HubSpot webhook body is too large",
        )

    raw_body = await request.body()
    if len(raw_body) > HUBSPOT_WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="HubSpot webhook body is too large",
        )

    try:
        # HubSpot v3 signs the exact method, public URL, raw body, and timestamp.
        # Parsing or rewriting the body first would change the bytes being
        # verified and could reject legitimate provider deliveries.
        verified = await verify_hubspot_v3_signature(
            method=request.method,
            request_uri=str(request.url),
            body=raw_body,
            timestamp=request.headers.get("x-hubspot-request-timestamp"),
            signature=request.headers.get("x-hubspot-signature-v3"),
        )
    except HubSpotWebhookVerificationError as exc:
        logger.warning("Rejected HubSpot webhook: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid HubSpot webhook signature",
        ) from exc

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid HubSpot webhook signature",
        )

    # Only verified payloads reach JSON parsing and queue fanout. That keeps
    # malformed unauthenticated traffic cheap and prevents noisy public requests
    # from creating integration-worker work.
    await ingestion_service.ingest(
        raw_body=raw_body,
        event_payloads=_parse_events(raw_body),
    )

    return Response(status_code=status.HTTP_200_OK)
