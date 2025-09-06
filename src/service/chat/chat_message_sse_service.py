from __future__ import annotations

"""SSE service for chat message job updates using Redis Streams.

Streams structured frames for a specific chat job over Server-Sent Events with
replay support (late join / reconnect) backed by a Redis Stream keyed per job.
"""
import asyncio
import json
from typing import AsyncGenerator, Optional, Dict
from uuid import UUID

from src.infra.messaging.redis_channels import channel_for_chat_job
from src.infra.messaging.redis_streams import AsyncRedisStreams
from src.infra.utils.logger import get_logger

logger = get_logger("chat_message_sse_service")


class ChatMessageSSEService:
    def __init__(self, organisation_id: UUID, job_id: UUID) -> None:
        self.organisation_id = str(organisation_id)
        self.job_id = str(job_id)
        self.stream_key = channel_for_chat_job(self.organisation_id, self.job_id)
        self._streams: Optional[AsyncRedisStreams] = None

    async def close(self) -> None:
        try:
            if self._streams:
                await self._streams.close()
        except Exception:  # pragma: no cover
            logger.debug("Redis streams close failed (chat SSE)", exc_info=True)

    async def stream_events(
        self,
        *,
        timeout_seconds: Optional[int] = 900,
        heartbeat_interval: float = 25.0,
        last_event_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict, None]:
        """Yield SSE events for a chat job from Redis Stream with replay support."""
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_heartbeat = start_time

        if self._streams is None:
            self._streams = AsyncRedisStreams()

        logger.info(
            "Chat SSE connect org=%s job=%s stream=%s resume=%s",
            self.organisation_id,
            self.job_id,
            self.stream_key,
            last_event_id,
        )

        # Initial ready event
        yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}

        next_id = last_event_id or "0-0"

        try:
            while True:
                now = loop.time()
                if (now - last_heartbeat) >= heartbeat_interval:
                    last_heartbeat = now
                    yield {"event": "ping", "data": json.dumps({"t": int(now)})}

                if timeout_seconds and (now - start_time) > timeout_seconds:
                    yield {
                        "event": "timeout",
                        "data": json.dumps(
                            {
                                "message": "Stream timeout",
                                "job_id": self.job_id,
                                "reconnect": True,
                            }
                        ),
                    }
                    return

                block_ms = int((heartbeat_interval / 2) * 1000)
                entries = await self._streams.xread(
                    self.stream_key, next_id, block_ms=block_ms, count=50
                )
                if not entries:
                    continue
                for entry_id, frame in entries:
                    next_id = entry_id
                    yield {"id": entry_id, "event": "frame", "data": json.dumps(frame)}
                    if frame.get("type") == "finalize":
                        yield {
                            "event": "complete",
                            "data": json.dumps({"job_id": self.job_id}),
                        }
                        return
        except Exception as e:
            logger.error("Chat SSE stream error: %s", e, exc_info=True)
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": "Internal stream error", "reconnect": True}
                ),
            }
        finally:
            await self.close()
