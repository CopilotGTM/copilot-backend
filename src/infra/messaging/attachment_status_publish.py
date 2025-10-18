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

from src.api.schemas.upload_models import AttachmentEventUpdate
from src.infra.messaging.publishers import RedisPublisher
from src.infra.messaging.redis_channels import channel_for_attachment_update_status
from src.infra.utils.logger import get_logger

logger = get_logger("attachment_publish")
_publisher = RedisPublisher(logger)


def publish_attachment_status(
    organisation_id: UUID | str,
    event: AttachmentEventUpdate,
):  # pragma: no cover (thin wrapper)
    """Sync Redis publish for attachment status updates with lazy client init.

    Workers may run outside FastAPI lifespan; lazily create redis client if missing.
    """
    payload = {
        "attachment_id": str(event.attachment_id),
        "status": event.status,
        "updated_at": event.updated_at,
        "error_message": event.error_message,
        "thumbnail_small_url": event.thumbnail_small_url,
    }
    channel = channel_for_attachment_update_status(organisation_id)
    _publisher.publish(channel, payload, context_id=str(event.attachment_id))
