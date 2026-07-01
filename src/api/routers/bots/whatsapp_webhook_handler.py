"""WhatsApp webhook handler.

Endpoints:
- GET  /bots/whatsapp/webhook  — Meta verification challenge
- POST /bots/whatsapp/webhook  — inbound webhook delivery

Notes:
- The endpoint does not use app authentication; it relies on app-secret
  signature verification (`X-Hub-Signature-256`) and a short dedupe key.
- Webhooks are handed off to background processing and return 200 quickly
  so Meta does not retry on transient worker scheduling delays.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from src.infra.config.configs import whatsapp_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.bots.auth_validators.whatsapp_auth import WhatsAppAuthValidator
from src.service.bots.whatsapp_bot_service import WhatsAppBotService

logger = get_logger(__name__)

router = APIRouter(tags=["whatsapp-bot"])


def _extract_message_ids(payload: dict[str, Any]) -> list[str]:
    """Collect WhatsApp message ids from a webhook payload."""
    message_ids: list[str] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                message_id = message.get("id")
                if message_id:
                    message_ids.append(str(message_id))
    return message_ids


@router.get("/webhook")
async def verify_whatsapp_webhook(
    mode: str = Query("", alias="hub.mode"),
    verify_token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    """Handle Meta's verification handshake.

    Meta posts these params during endpoint registration; if they match the
    configured token, we return the challenge verbatim.
    """
    if not whatsapp_config.WHATSAPP_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp integration is not enabled",
        )

    expected_verify_token = await whatsapp_config.get_webhook_verify_token()
    if mode != "subscribe" or not expected_verify_token or verify_token != expected_verify_token:
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    return Response(content=challenge, media_type="text/plain", status_code=status.HTTP_200_OK)


@router.post("/webhook")
async def handle_whatsapp_webhook(request: Request) -> Response:
    """Handle incoming WhatsApp callbacks and enqueue async message processing."""
    if not whatsapp_config.WHATSAPP_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp integration is not enabled",
        )

    body = await request.body()
    app_secret = await whatsapp_config.get_app_secret()
    auth_result = await WhatsAppAuthValidator(app_secret).validate(request.headers, body)
    if not auth_result.is_valid:
        logger.warning("WhatsApp webhook auth failed: %s", auth_result.error)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    # Parse raw JSON body before any async queueing so we fail fast on bad payloads.
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from exc

    message_ids = _extract_message_ids(payload)
    if message_ids:
        # Best-effort idempotency gate: ignore a retry if we've already seen these ids.
        try:
            redis = get_redis_client()
            new_message_seen = False
            for message_id in message_ids:
                was_new = redis.set(
                    f"webhook:whatsapp:{message_id}",
                    "1",
                    nx=True,
                    ex=whatsapp_config.WEBHOOK_DEDUPE_TTL_SECONDS,
                )
                new_message_seen = new_message_seen or bool(was_new)
            if not new_message_seen:
                logger.info("Duplicate WhatsApp webhook ignored message_ids=%s", message_ids)
                return Response(status_code=status.HTTP_200_OK)
        except Exception:
            logger.debug("WhatsApp webhook dedupe check failed", exc_info=True)

    service = WhatsAppBotService()

    # Do not return 200 until the inbound message has crossed a durable boundary.
    # A detached task can be cancelled by API pod shutdown after creating the DB
    # job but before publishing the Celery task, leaving a permanently pending job.
    try:
        await service.process_webhook(payload)
    except Exception:
        logger.error("Error processing WhatsApp webhook", exc_info=True)
    return Response(status_code=status.HTTP_200_OK)
