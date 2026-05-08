from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from src.infra.config.configs import hubspot_integration_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.hubspot.constants import HUBSPOT_WEBHOOK_EVENT_TASK
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
HUBSPOT_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60


def _request_uri_for_signature(request: Request) -> str:
    configured_url = getattr(hubspot_integration_config, "WEBHOOK_PUBLIC_URL", "")
    if configured_url:
        return configured_url
    return str(request.url)


def _parse_events(raw_body: bytes) -> list[dict[str, Any]]:
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
    if event.event_id:
        return f"webhook:hubspot:{event.portal_id}:{event.event_id}"

    body_hash = hashlib.sha256(raw_body).hexdigest()
    return (
        f"webhook:hubspot:{event.portal_id}:{event.subscription_type}:{event.object_id}:{body_hash}"
    )


def _is_duplicate_event(event: HubSpotWebhookEvent, raw_body: bytes) -> bool:
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
        logger.debug("HubSpot webhook dedupe check failed", exc_info=True)
        return False


@router.post("/webhook")
async def handle_hubspot_webhook(request: Request) -> Response:
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

    _, client_secret = await hubspot_integration_config.get_credentials()
    try:
        verified = verify_hubspot_v3_signature(
            client_secret=client_secret,
            method=request.method,
            request_uri=_request_uri_for_signature(request),
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

    events = _parse_events(raw_body)
    for event_payload in events:
        event = HubSpotWebhookEvent.from_payload(event_payload)
        if event is None:
            logger.warning(
                "Ignoring malformed HubSpot webhook event",
                extra={"event": "hubspot_webhook_event_malformed"},
            )
            continue

        if not HubSpotWebhookDispatcher.is_supported(event.subscription_type):
            continue

        if _is_duplicate_event(event, raw_body):
            logger.info(
                "Duplicate HubSpot webhook event ignored",
                extra={
                    "event": "hubspot_webhook_event_duplicate",
                    "subscription_type": event.subscription_type,
                    "portal_id": event.portal_id,
                    "object_id": event.object_id,
                    "event_id": event.event_id,
                },
            )
            continue

        task_id = await send_integrations_task_async(
            task_name=HUBSPOT_WEBHOOK_EVENT_TASK,
            kwargs=event.to_task_kwargs(),
            queue="integrations_priority",
            routing_key="integrations_priority",
        )
        logger.info(
            "Queued HubSpot webhook event",
            extra={
                "event": "hubspot_webhook_event_queued",
                "subscription_type": event.subscription_type,
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
                "task_id": task_id,
            },
        )

    return Response(status_code=status.HTTP_200_OK)
