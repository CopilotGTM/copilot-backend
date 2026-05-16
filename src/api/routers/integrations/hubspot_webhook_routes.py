"""Public HubSpot webhook endpoint.

HubSpot calls this unauthenticated route directly, so the route owns signature
verification, event-shape validation, dedupe, and fanout to the integrations
worker. The worker owns provider-specific writes after the event is safely
queued.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.hubspot.constants import HUBSPOT_WEBHOOK_EVENT_TASK
from src.service.integrations.webhooks.hubspot.deals.subscriptions import (
    DEAL_CREATION,
    DEAL_DELETION,
    DEAL_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.dispatcher import HubSpotWebhookDispatcher
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.service.integrations.webhooks.hubspot.verifier import (
    HubSpotWebhookVerificationError,
    verify_hubspot_v3_signature,
)
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)

router = APIRouter(prefix="/hubspot", tags=["integrations", "hubspot-webhooks"])

HUBSPOT_WEBHOOK_MAX_BODY_BYTES = 256 * 1024
# HubSpot retries for transient delivery failures; keep the dedupe window long
# enough to cover normal replay bursts without suppressing next-day changes.
HUBSPOT_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60
_DEAL_EVENT_PRIORITY = {
    DEAL_PROPERTY_CHANGE: 1,
    DEAL_CREATION: 2,
    DEAL_DELETION: 3,
}


def _parse_events(raw_body: bytes) -> list[dict[str, Any]]:
    """Normalize HubSpot's single-event or batch payload into a list."""
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


def _dedupe_key(event: HubSpotWebhookEvent, raw_body: bytes) -> str:
    """Build the idempotency key for a webhook event."""
    if event.event_id:
        return f"webhook:hubspot:{event.portal_id}:{event.event_id}"

    # Some HubSpot event shapes are missing eventId, so the raw body hash keeps
    # retries idempotent without collapsing distinct changes for the same object.
    body_hash = hashlib.sha256(raw_body).hexdigest()
    return (
        f"webhook:hubspot:{event.portal_id}:{event.subscription_type}:{event.object_id}:{body_hash}"
    )


def _event_property_name(event: HubSpotWebhookEvent) -> str | None:
    property_name = event.raw.get("propertyName")
    return str(property_name) if property_name is not None else None


def _coalesce_batch_events(events: list[HubSpotWebhookEvent]) -> list[HubSpotWebhookEvent]:
    coalesced: list[HubSpotWebhookEvent] = []
    deal_indexes: dict[tuple[str, str], int] = {}

    for event in events:
        event_priority = _DEAL_EVENT_PRIORITY.get(event.subscription_type)
        if event_priority is None:
            coalesced.append(event)
            continue

        deal_key = (event.portal_id, event.object_id)
        existing_index = deal_indexes.get(deal_key)
        if existing_index is None:
            deal_indexes[deal_key] = len(coalesced)
            coalesced.append(event)
            continue

        existing_event = coalesced[existing_index]
        existing_priority = _DEAL_EVENT_PRIORITY.get(existing_event.subscription_type, 0)
        selected_event = existing_event
        ignored_event = event
        if event_priority > existing_priority:
            selected_event = event
            ignored_event = existing_event
            coalesced[existing_index] = event

        logger.info(
            "Coalesced HubSpot deal webhook event",
            extra={
                "event": "hubspot_webhook_event_coalesced",
                "subscription_type": ignored_event.subscription_type,
                "selected_subscription_type": selected_event.subscription_type,
                "portal_id": ignored_event.portal_id,
                "object_id": ignored_event.object_id,
                "event_id": ignored_event.event_id,
                "selected_event_id": selected_event.event_id,
                "property_name": _event_property_name(ignored_event),
            },
        )

    return coalesced


def _is_duplicate_event(event: HubSpotWebhookEvent, raw_body: bytes) -> bool:
    """Claim the webhook event in Redis and report whether it was already seen."""
    try:
        redis = get_redis_client()
        was_set = redis.set(
            _dedupe_key(event, raw_body),
            "1",
            nx=True,
            ex=HUBSPOT_WEBHOOK_DEDUPE_TTL_SECONDS,
        )
        return not bool(was_set)
    except Exception:
        # Dedupe is an optimization, not the security boundary. If Redis is
        # unavailable, let the worker handle the event instead of dropping it.
        logger.debug("HubSpot webhook dedupe check failed", exc_info=True)
        return False


def _release_dedupe_claim(event: HubSpotWebhookEvent, raw_body: bytes) -> None:
    """Release a dedupe claim when enqueueing fails so HubSpot can retry."""
    try:
        redis = get_redis_client()
        redis.delete(_dedupe_key(event, raw_body))
    except Exception:
        logger.debug("HubSpot webhook dedupe release failed", exc_info=True)


@router.post("/webhook")
async def handle_hubspot_webhook(request: Request) -> Response:
    """Validate and enqueue HubSpot webhook events.

    The route parses events only after signature verification, then drops
    unsupported subscriptions before Celery fanout so unsupported HubSpot app
    subscriptions cannot create worker noise.
    """
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
        # Verification uses the exact raw body and public URI; parsing or
        # rewriting either first would invalidate HubSpot's v3 signature.
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

    events = []
    for event_payload in _parse_events(raw_body):
        event = HubSpotWebhookEvent.from_payload(event_payload)
        if event is None:
            logger.warning(
                "Ignoring malformed HubSpot webhook event",
                extra={"event": "hubspot_webhook_event_malformed"},
            )
            continue

        if not HubSpotWebhookDispatcher.is_supported(event.subscription_type):
            continue

        events.append(event)

    for event in _coalesce_batch_events(events):
        if _is_duplicate_event(event, raw_body):
            logger.info(
                "Duplicate HubSpot webhook event ignored",
                extra={
                    "event": "hubspot_webhook_event_duplicate",
                    "subscription_type": event.subscription_type,
                    "portal_id": event.portal_id,
                    "object_id": event.object_id,
                    "event_id": event.event_id,
                    "property_name": _event_property_name(event),
                },
            )
            continue

        try:
            # Claim dedupe before enqueue, then release only on broker failure.
            # That keeps successful webhook retries from double-writing while
            # still allowing HubSpot to redeliver if we never reached Celery.
            task_id = await send_integrations_task_async(
                task_name=HUBSPOT_WEBHOOK_EVENT_TASK,
                kwargs=event.to_task_kwargs(),
                queue="integrations_priority",
                routing_key="integrations_priority",
            )
        except Exception:
            _release_dedupe_claim(event, raw_body)
            raise
        logger.info(
            "Queued HubSpot webhook event",
            extra={
                "event": "hubspot_webhook_event_queued",
                "subscription_type": event.subscription_type,
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
                "property_name": _event_property_name(event),
                "task_id": task_id,
            },
        )

    return Response(status_code=status.HTTP_200_OK)
