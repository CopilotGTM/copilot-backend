"""Recall webhook ingress for meeting recording state changes.

This route does only the request-bound work that must happen before we hand off
to Celery: parse the raw body, verify the provider signature, and dedupe obvious
replays. All session updates happen in the worker so API latency stays low.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response, status
from src.infra.config.configs import recall_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.integrations.meeting_recording.providers import (
    RecallWebhookVerificationError,
    verify_recall_webhook_signature,
)
from src.workers.deal_worker.client import send_deal_task_async

logger = get_logger(__name__)

RECALL_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60

router = APIRouter(tags=["meeting-bot"])


@router.post("/webhook")
async def handle_recall_webhook(request: Request) -> Response:
    """Receive Recall workspace webhooks and hand off processing asynchronously."""
    body = await request.body()

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from exc

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
        logger.warning("Recall webhook signature verification failed: %s", exc)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    webhook_id = request.headers.get("webhook-id") or request.headers.get("svix-id")
    if webhook_id:
        try:
            redis = get_redis_client()
            # The queue worker also takes a lock, but rejecting obvious retries
            # here avoids duplicate Celery load during provider re-deliveries.
            deduped = redis.set(
                f"webhook:recall:{webhook_id}",
                "1",
                nx=True,
                ex=RECALL_WEBHOOK_DEDUPE_TTL_SECONDS,
            )
            if not deduped:
                logger.info("Duplicate Recall webhook ignored: webhook_id=%s", webhook_id)
                return Response(status_code=status.HTTP_200_OK)
        except Exception:
            logger.debug("Recall webhook dedupe check failed", exc_info=True)

    # Background processing owns the org lookup, session mutation, and any
    # provider follow-up fetches so the HTTP handler remains cheap and idempotent.
    try:
        await send_deal_task_async(
            "deal.process_meeting_recording_webhook",
            kwargs={
                "payload": payload,
                "webhook_id": webhook_id,
            },
        )
    except Exception as exc:
        logger.warning(
            "Failed to queue meeting recording webhook task",
            extra={"webhook_id": webhook_id},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to queue webhook processing task",
        ) from exc

    return Response(status_code=status.HTTP_200_OK)
