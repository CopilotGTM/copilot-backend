"""Redis Streams utilities (async + sync) for durable, replayable event delivery.

Provides a thin abstraction similar in spirit to `redis_pubsub.AsyncRedisPubSub` but
for Redis Streams, enabling late join / reconnect replay and bounded history.

Usage patterns:

Sync producer (e.g. Celery worker):
    from src.infra.messaging.redis_streams import xadd_json_frame
    xadd_json_frame(stream_key, {"type": "answer.delta", ...}, maxlen=500)

Async consumer (e.g. SSE service):
    streams = AsyncRedisStreams()
    async for entry_id, frame in streams.read_forever(stream_key, start_id="0-0"):
        ...

Low‑level one shot read:
    entries = await streams.xread(stream_key, last_id, block_ms=5000, count=50)

Notes:
- We store the frame JSON under a single field name 'data' to minimize per-entry overhead.
- `maxlen` uses approximate trimming for performance (`~`).
- Call `close()` in long‑lived async contexts to free connections.
"""
from __future__ import annotations

from typing import List, Tuple, Optional, AsyncGenerator, Dict, Any
import json

try:  # redis >=4.2
    import redis.asyncio as redis_async  # type: ignore
except ImportError:  # pragma: no cover
    redis_async = None  # type: ignore

from src.infra.messaging.redis_client import get_redis_client  # sync client
from src.infra.config.configs import Config
from src.infra.utils.logger import get_logger

logger = get_logger("redis_streams")

__all__ = [
    "AsyncRedisStreams",
    "xadd_json_frame",
]

# NOTE:
# Channel naming helpers have been centralized in `redis_channels` and are no longer
# re-exported here. Import them explicitly when needed:
#   from src.infra.messaging.redis_channels import channel_for_chat_job

DATA_FIELD = "data"

class AsyncRedisStreams:
    """Async Redis Streams helper.

    Provides minimal methods needed for SSE consumption with replay.
    """
    def __init__(self):
        if redis_async is None:
            raise RuntimeError("Async redis support not available. Install 'redis>=5.0.0'.")
        self.config = Config()
        self._redis = None

    async def _client(self):
        if self._redis is None:
            redis_host, redis_port, redis_password = self.config.get_redis_client_details
            ssl_enabled = False if self.config.env == "local" else True
            self._redis = redis_async.Redis(  # type: ignore[attr-defined]
                host=redis_host,
                port=redis_port,
                password=redis_password,
                decode_responses=True,
                socket_timeout=None,
                socket_connect_timeout=30,
                retry_on_timeout=True,
                ssl=ssl_enabled,
                socket_keepalive=True,
                health_check_interval=0,
            )
            await self._redis.ping()
        return self._redis

    async def xread(self, stream_key: str, last_id: str, *, block_ms: int = 5000, count: int = 50) -> List[Tuple[str, Dict[str, Any]]]:
        """Read entries > last_id (use '0-0' for from start). Returns list of (id, parsed_json)."""
        client = await self._client()
        try:
            result = await client.xread({stream_key: last_id}, count=count, block=block_ms)
        except Exception as e:
            logger.error("XREAD error on %s: %s", stream_key, e)
            raise
        entries: List[Tuple[str, Dict[str, Any]]] = []
        for _stream, items in result:
            for entry_id, fields in items:
                raw = fields.get(DATA_FIELD)
                if raw is None:
                    continue
                try:
                    frame = json.loads(raw)
                except Exception:
                    logger.warning("Failed to parse stream frame id=%s", entry_id)
                    continue
                entries.append((entry_id, frame))
        return entries

    async def read_forever(
        self,
        stream_key: str,
        *,
        start_id: str = "0-0",
        block_ms: int = 5000,
        count: int = 50,
    ) -> AsyncGenerator[Tuple[str, Dict[str, Any]], None]:
        """Yield (id, frame) continuously starting from start_id (inclusive semantics as per XREAD)."""
        last_id = start_id
        while True:
            entries = await self.xread(stream_key, last_id, block_ms=block_ms, count=count)
            if not entries:
                continue
            for entry_id, frame in entries:
                last_id = entry_id
                yield entry_id, frame

    async def close(self):  # parity with pubsub helper
        try:
            if self._redis:
                await self._redis.close()
        except Exception:
            logger.debug("Error closing async redis streams client", exc_info=True)
        finally:
            self._redis = None

# Sync producer convenience --------------------------------------------------

def xadd_json_frame(stream_key: str, frame: Dict[str, Any], *, maxlen: int = 500, approximate: bool = True) -> Optional[str]:
    """Add a JSON frame to a Redis Stream using sync client.

    Returns the generated entry ID or None if failure.
    """
    client = get_redis_client()
    try:
        return client.xadd(
            stream_key,
            {DATA_FIELD: json.dumps(frame)},
            maxlen=maxlen,
            approximate=approximate,
        )
    except Exception as e:  # pragma: no cover (network issues)
        logger.warning("Failed to XADD to %s: %s", stream_key, e)
        return None
