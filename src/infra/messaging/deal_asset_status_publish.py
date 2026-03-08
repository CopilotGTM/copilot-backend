"""Helpers to publish deal asset status updates to Redis.

Channel naming:
  deal_asset:{organisation_id}:assets -> fan-out for all deal asset status changes in an org.

Payload (JSON):
  {
    "asset_id": str,
    "deal_id": str,
    "status": str,
    "updated_at": iso8601 or null,
    "error_message": str | null,
    "thumbnail_small_url": str | null
  }
"""

from __future__ import annotations

from uuid import UUID

from src.infra.messaging.event_models import DealAssetEventUpdate
from src.infra.messaging.publishers import RedisPublisher
from src.infra.messaging.redis_channels import channel_for_deal_asset_status
from src.infra.utils.logger import get_logger

logger = get_logger("deal_asset_publish")
_publisher = RedisPublisher(logger)


def publish_deal_asset_status(
    organisation_id: UUID | str,
    event: DealAssetEventUpdate,
):  # pragma: no cover (thin wrapper)
    """Sync Redis publish for deal asset status updates with lazy client init."""
    payload = {
        "asset_id": str(event.asset_id),
        "deal_id": str(event.deal_id),
        "status": event.status,
        "updated_at": event.updated_at,
        "error_message": event.error_message,
        "thumbnail_small_url": event.thumbnail_small_url,
    }
    channel = channel_for_deal_asset_status(organisation_id)
    _publisher.publish(channel, payload, context_id=str(event.asset_id))
