"""Recall webhook ingress for meeting recording state changes.

This route does only the request-bound work that must happen before we hand off
to Celery: bound and parse the raw body, then verify the provider signature.
All session updates and idempotency checks happen in the worker so a failed task
does not cause an ingress dedupe key to suppress Recall's retry.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response, status
from src.infra.config.configs import recall_config
from src.infra.security.request_body import read_bounded_request_body
from src.infra.utils.logger import get_logger
from src.service.meeting_bot.providers import (
    RecallWebhookVerificationError,
    verify_recall_webhook_signature,
)
from src.workers.deal_worker.client import send_deal_task_async

logger = get_logger(__name__)

# Provider envelopes are small; this cap prevents an unauthenticated chunked
# request from being buffered without limit before signature verification.
RECALL_WEBHOOK_MAX_BODY_BYTES = 256 * 1024

router = APIRouter(tags=["meeting-bot"])


def _signature_versions(signature_header: str | None) -> list[str]:
    """Expose only signature scheme names for debugging, never signature values."""
    versions: list[str] = []
    for part in (signature_header or "").split():
        version = part.split(",", 1)[0]
        if version:
            versions.append(version)
    return versions


@router.post("/webhook")
async def handle_recall_webhook(request: Request) -> Response:
    """Receive Recall workspace webhooks and hand off processing asynchronously."""
    body = await read_bounded_request_body(
        request,
        max_bytes=RECALL_WEBHOOK_MAX_BODY_BYTES,
        label="Recall webhook",
    )

    # Fail closed: if signature verification is not configured we do not accept
    # unauthenticated provider traffic.
    webhook_secret = await recall_config.get_webhook_secret()
    if not webhook_secret:
        logger.error("Recall webhook received but RECALL_WEBHOOK_SECRET is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recall webhook verification is not configured",
        )

    try:
        # Recall signs the exact raw request body, so verification must happen
        # before any normalization beyond UTF-8 decoding for JSON parsing.
        verify_recall_webhook_signature(
            secret=webhook_secret,
            headers=request.headers,
            payload=body,
        )
    except RecallWebhookVerificationError as exc:
        logger.warning(
            "Recall webhook signature verification failed: %s",
            exc,
            extra={
                "event": "recall_webhook_signature_verification_failed",
                "has_webhook_id": bool(
                    request.headers.get("webhook-id") or request.headers.get("svix-id")
                ),
                "has_webhook_timestamp": bool(
                    request.headers.get("webhook-timestamp")
                    or request.headers.get("svix-timestamp")
                ),
                "signature_versions": _signature_versions(
                    request.headers.get("webhook-signature")
                    or request.headers.get("svix-signature")
                ),
                "payload_size_bytes": len(body),
                "webhook_secret_has_whsec_prefix": webhook_secret.strip().startswith("whsec_"),
            },
        )
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body must be a JSON object",
        )

    event_name = str(payload.get("event") or "")
    data = payload.get("data") or {}
    bot = (data.get("bot") or {}) if isinstance(data, dict) else {}
    bot_id = bot.get("id") if isinstance(bot, dict) else None
    bot_metadata = bot.get("metadata") if isinstance(bot, dict) else None
    organisation_id = (
        bot_metadata.get("organisation_id") if isinstance(bot_metadata, dict) else None
    )

    webhook_id = request.headers.get("webhook-id") or request.headers.get("svix-id")

    # Background processing owns the org lookup, session mutation, and any
    # provider follow-up fetches so the HTTP handler remains cheap and idempotent.
    try:
        task_id = await send_deal_task_async(
            "deal.process_meeting_recording_webhook",
            kwargs={
                "payload": payload,
                "webhook_id": webhook_id,
            },
        )
        logger.info(
            "Queued Recall webhook for worker processing",
            extra={
                "event": "recall_webhook_task_queued",
                "webhook_id": webhook_id,
                "recall_event": event_name,
                "provider_session_id": bot_id if isinstance(bot_id, str) else None,
                "organisation_id": organisation_id if isinstance(organisation_id, str) else None,
                "celery_task_id": task_id,
                "celery_task_name": "deal.process_meeting_recording_webhook",
                "celery_queue": "deal",
            },
        )
    except Exception as exc:
        logger.warning(
            "Failed to queue meeting recording webhook task",
            extra={
                "event": "recall_webhook_task_queue_failed",
                "webhook_id": webhook_id,
                "recall_event": event_name,
                "provider_session_id": bot_id if isinstance(bot_id, str) else None,
                "organisation_id": organisation_id if isinstance(organisation_id, str) else None,
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to queue webhook processing task",
        ) from exc

    return Response(status_code=status.HTTP_200_OK)
