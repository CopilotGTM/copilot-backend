"""WhatsApp webhook handler.

Endpoints:
- GET  /bots/whatsapp/webhook  — Meta verification challenge
- POST /bots/whatsapp/webhook  — inbound webhook delivery

Notes:
- The endpoint does not use app authentication; it relies on app-secret
  signature verification (`X-Hub-Signature-256`).
- Linked inbound messages return success only after their database job is durable.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from src.infra.config.configs import whatsapp_config
from src.infra.observability.metrics import record_whatsapp_inbound
from src.infra.utils.logger import get_logger
from src.service.bots.auth_validators.whatsapp_auth import WhatsAppAuthValidator
from src.service.bots.whatsapp_bot_service import (
    WhatsAppBotService,
    WhatsAppInboundDurabilityError,
)

logger = get_logger(__name__)

router = APIRouter(tags=["whatsapp-bot"])


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
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_whatsapp_inbound("invalid")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from exc
    if not isinstance(payload, dict):
        record_whatsapp_inbound("invalid")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        )

    service = WhatsAppBotService()

    # Do not return 200 until the inbound message has crossed a durable boundary.
    # A detached task can be cancelled by API pod shutdown after creating the DB
    # job but before publishing the Celery task, leaving a permanently pending job.
    try:
        await service.process_webhook(payload)
    except WhatsAppInboundDurabilityError:
        # Meta must retry while the linked message has no reconstructable DB job.
        logger.error("WhatsApp webhook did not reach the durable boundary", exc_info=True)
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_200_OK)
