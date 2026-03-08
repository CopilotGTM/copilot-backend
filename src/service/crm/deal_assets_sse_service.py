"""SSE stream service for deal asset status updates.

Subscribes to Redis pub/sub channel for deal asset events and streams
status updates to the frontend. Follows the same pattern as
AttachmentSSEService but scoped to deal assets.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from src.infra.messaging.redis_channels import channel_for_deal_asset_status
from src.infra.messaging.redis_pubsub import AsyncRedisPubSub
from src.infra.utils.logger import get_logger

logger = get_logger("deal_asset_sse_service")


class DealAssetSSEService:
    TERMINAL_STATUSES = {"active", "failed"}

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.redis_pubsub = AsyncRedisPubSub()

    async def close(self):  # pragma: no cover
        try:
            await self.redis_pubsub.close()
        except Exception:
            logger.debug("Redis pubsub close failed (DealAsset SSE)", exc_info=True)

    def _send_event(self, event_type: str, data) -> dict:
        return {"event": event_type, "data": json.dumps(data)}

    async def stream_events(
        self,
        deal_id: UUID | None = None,
        timeout_seconds: int | None = 1800,
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[dict, None]:
        """Stream deal asset status events via Redis pub/sub.

        If deal_id is provided, only events for that deal are forwarded.
        """
        logger.info(
            "Starting deal asset SSE stream org=%s deal=%s",
            self.organisation_id,
            deal_id,
        )
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time
        deal_id_str = str(deal_id) if deal_id else None

        try:
            channel = channel_for_deal_asset_status(self.organisation_id)
            async for message in self.redis_pubsub.subscribe_to_channel(channel):
                current_time = asyncio.get_event_loop().time()

                if timeout_seconds and (current_time - start_time) > timeout_seconds:
                    yield self._send_event("timeout", {"message": "timeout", "reconnect": True})
                    return

                if (current_time - last_heartbeat) >= heartbeat_interval:
                    last_heartbeat = current_time
                    yield self._send_event("ping", {"timestamp": int(current_time)})

                # Filter by deal_id if specified
                if deal_id_str and message.get("deal_id") != deal_id_str:
                    continue

                yield self._send_event("status", message)

        except Exception as e:
            logger.error("Deal asset SSE stream error: %s", e, exc_info=True)
            yield self._send_event("error", {"message": "internal_error", "reconnect": True})
        finally:
            try:
                await self.redis_pubsub.close()
            except Exception:
                logger.debug("Redis pubsub close failed in finally (DealAsset SSE)")
