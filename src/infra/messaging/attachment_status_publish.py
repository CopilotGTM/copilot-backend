"""Helpers to publish chat attachment status updates to Redis.

Channel naming:
  att:{organisation_id}:attachments -> fan-out for all attachment status changes in an org.

Payload (JSON):
  {
    "attachment_id": str,
    "status": str,
    "updated_at": iso8601 or null,
    "error_message": str | null,
    "thumbnail_small_url": str | null
  }

Lightweight (only core fields). API layer / SSE service may enrich further.
"""
from __future__ import annotations

from uuid import UUID
import json

from src.api.schemas.upload_models import AttachmentEventUpdate
from src.infra.utils.app_state import fastapp
from src.infra.utils.logger import get_logger
from src.infra.messaging.redis_client import get_redis_client
from src.infra.messaging.redis_pubsub import channel_for_attachment_update_status

logger = get_logger("attachment_publish")


def publish_attachment_status(
    organisation_id: UUID | str,
    event: AttachmentEventUpdate,
):  # pragma: no cover (thin wrapper)
    """Sync Redis publish for attachment status updates with lazy client init.

    Workers may run outside FastAPI lifespan; lazily create redis client if missing.
    """
    try:
        redis_client = getattr(fastapp.state, "redis_client", None)
        if redis_client is None:
            redis_client = get_redis_client()
            fastapp.state.redis_client = redis_client
    except Exception as e:  # pragma: no cover
        logger.warning("Redis client unavailable; skipping attachment publish for %s", event.attachment_id)
        logger.debug("Redis init failure detail: %s", e)
        return

    payload = {
        "attachment_id": str(event.attachment_id),
        "status": event.status,
        "updated_at": event.updated_at,
        "error_message": event.error_message,
        "thumbnail_small_url": event.thumbnail_small_url,
    }
    try:
        channel = channel_for_attachment_update_status(organisation_id)
        redis_client.publish(channel, json.dumps(payload))
        logger.debug("Published attachment update for %s: %s", event.attachment_id, event.status)
    except Exception as exc:
        logger.warning("Failed to publish attachment status for %s: %s", event.attachment_id, exc)
