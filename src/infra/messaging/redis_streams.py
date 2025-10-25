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

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import redis.asyncio as redis_async
else:
    try:  # redis >=4.2
        import redis.asyncio as redis_async  # type: ignore
    except ImportError:  # pragma: no cover
        redis_async = None  # type: ignore

from src.infra.config.configs import Config
from src.infra.messaging.redis_client import get_redis_client  # sync client
from src.infra.utils.logger import get_logger

logger = get_logger("redis_streams")

__all__ = [
    "AsyncRedisStreams",
    "xadd_json_frame",
    "initialize_async_redis_pool",
    "close_async_redis_pool",
]

# NOTE:
# Channel naming helpers have been centralized in `redis_channels` and are no longer
# re-exported here. Import them explicitly when needed:
#   from src.infra.messaging.redis_channels import channel_for_chat_job

DATA_FIELD = "data"


class AsyncRedisConnectionManager:
    """Async Redis connection pool manager."""

    _instance: AsyncRedisConnectionManager | None = None
    _async_pool: object | None = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def initialize(self) -> None:
        """
        Explicitly initialize the async Redis connection pool.
        Safe to call multiple times - will only initialize once.
        Call this during application/worker startup.
        """
        if not self._initialized:
            await self._initialize_pool()
            self._initialized = True

    async def get_client(self):
        """Get async Redis client with connection pooling."""
        if self._async_pool is None:
            await self._initialize_pool()
            self._initialized = True
        return redis_async.Redis(connection_pool=self._async_pool)

    async def _initialize_pool(self) -> None:
        """Initialize async Redis connection pool."""
        if redis_async is None:
            raise RuntimeError("Async redis support not available. Install 'redis>=6.0.0'.")

        config = Config()
        redis_host, redis_port, redis_password = config.get_redis_client_details

        # Determine if SSL should be used (enabled for non-local hosts)
        ssl_enabled = not (
            redis_host in ["localhost", "127.0.0.1"] or redis_host.startswith("192.168.")
        )

        # Use rediss:// protocol for SSL, redis:// for non-SSL
        protocol = "rediss" if ssl_enabled else "redis"
        redis_url = f"{protocol}://:{redis_password}@{redis_host}:{redis_port}"

        # Create connection pool using from_url for proper SSL handling
        self._async_pool = redis_async.ConnectionPool.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=None,
            socket_connect_timeout=30,
            retry_on_timeout=True,
            socket_keepalive=True,
            health_check_interval=0,
            max_connections=20,
        )

        # Test the connection
        test_client = redis_async.Redis(connection_pool=self._async_pool)
        try:
            await test_client.ping()
            logger.info(
                "Async Redis connection pool initialized successfully (host=%s, ssl=%s)",
                redis_host,
                ssl_enabled,
            )
        except Exception as e:
            logger.error("Failed to initialize async Redis connection pool: %s", e)
            self._async_pool = None
            self._initialized = False
            raise
        finally:
            await test_client.close()

    async def close_pool(self) -> None:
        """Close the async connection pool."""
        if self._async_pool:
            await self._async_pool.disconnect()
            self._async_pool = None
            self._initialized = False
            logger.info("Async Redis connection pool closed")


# Global async connection manager instance
_async_redis_manager = AsyncRedisConnectionManager()


class AsyncRedisStreams:
    """Async Redis Streams helper with connection pooling.

    Provides minimal methods needed for SSE consumption with replay.
    """

    def __init__(self):
        self._redis = None

    async def _client(self):
        """Get Redis client from connection pool."""
        return await _async_redis_manager.get_client()

    async def xread(
        self, stream_key: str, last_id: str, *, block_ms: int = 5000, count: int = 50
    ) -> list[tuple[str, dict[str, Any]]]:
        """Read entries > last_id (use '0-0' for from start). Returns list of (id, parsed_json)."""
        client = await self._client()
        try:
            result = await client.xread({stream_key: last_id}, count=count, block=block_ms)
        except Exception as e:
            logger.error("XREAD error on %s: %s", stream_key, e)
            raise
        entries: list[tuple[str, dict[str, Any]]] = []
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

    async def create_consumer_group(
        self, stream_key: str, group_name: str, start_id: str = "0"
    ) -> bool:
        """Create a consumer group for the stream. Returns True if created, False if already exists."""
        client = await self._client()
        try:
            await client.xgroup_create(stream_key, group_name, start_id, mkstream=True)
            logger.info("Created consumer group %s for stream %s", group_name, stream_key)
            return True
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.debug(
                    "Consumer group %s already exists for stream %s",
                    group_name,
                    stream_key,
                )
                return False
            logger.error(
                "Failed to create consumer group %s for stream %s: %s",
                group_name,
                stream_key,
                e,
            )
            raise

    async def xreadgroup(
        self,
        stream_key: str,
        group_name: str,
        consumer_name: str,
        *,
        start_id: str = ">",
        block_ms: int = 5000,
        count: int = 50,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Read from stream using consumer group. Messages are delivered only once per group."""
        client = await self._client()
        try:
            result = await client.xreadgroup(
                group_name,
                consumer_name,
                {stream_key: start_id},
                count=count,
                block=block_ms,
            )
        except Exception as e:
            logger.error(
                "XREADGROUP error on %s (group=%s, consumer=%s): %s",
                stream_key,
                group_name,
                consumer_name,
                e,
            )
            raise

        entries: list[tuple[str, dict[str, Any]]] = []
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

    async def xack(self, stream_key: str, group_name: str, *entry_ids: str) -> int:
        """Acknowledge messages as processed. Returns number of acknowledged messages."""
        if not entry_ids:
            return 0

        client = await self._client()
        try:
            return await client.xack(stream_key, group_name, *entry_ids)
        except Exception as e:
            logger.error("XACK error on %s (group=%s): %s", stream_key, group_name, e)
            raise

    async def delete_stream(self, stream_key: str) -> bool:
        """Delete the entire stream. Returns True if deleted."""
        client = await self._client()
        try:
            result = await client.delete(stream_key)
            if result:
                logger.info("Deleted stream %s", stream_key)
            return bool(result)
        except Exception as e:
            logger.error("Failed to delete stream %s: %s", stream_key, e)
            raise

    async def read_forever(
        self,
        stream_key: str,
        *,
        start_id: str = "0-0",
        block_ms: int = 5000,
        count: int = 50,
    ) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        """Yield (id, frame) continuously starting from start_id (inclusive semantics as per XREAD)."""
        last_id = start_id
        while True:
            entries = await self.xread(stream_key, last_id, block_ms=block_ms, count=count)
            if not entries:
                continue
            for entry_id, frame in entries:
                last_id = entry_id
                yield entry_id, frame

    async def read_forever_consumer_group(
        self,
        stream_key: str,
        group_name: str,
        consumer_name: str,
        *,
        auto_ack: bool = True,
        block_ms: int = 5000,
        count: int = 50,
    ) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        """
        Yield (id, frame) continuously using consumer group.
        Messages are delivered only once per consumer group.

        Args:
            stream_key: Redis stream key
            group_name: Consumer group name (e.g., 'sse_consumers')
            consumer_name: Unique consumer name (e.g., user_id or session_id)
            auto_ack: If True, automatically acknowledge messages after yielding
            block_ms: Blocking timeout in milliseconds
            count: Maximum messages to read per batch
        """
        # Ensure consumer group exists
        await self.create_consumer_group(stream_key, group_name)

        while True:
            entries = await self.xreadgroup(
                stream_key, group_name, consumer_name, block_ms=block_ms, count=count
            )

            if not entries:
                continue

            entry_ids_to_ack = []
            for entry_id, frame in entries:
                yield entry_id, frame
                if auto_ack:
                    entry_ids_to_ack.append(entry_id)

            # Acknowledge processed messages if auto_ack is enabled
            if auto_ack and entry_ids_to_ack:
                try:
                    await self.xack(stream_key, group_name, *entry_ids_to_ack)
                except Exception as e:
                    logger.error("Failed to acknowledge messages: %s", e)

    async def close(self):  # parity with pubsub helper
        try:
            if self._redis:
                await self._redis.close()
        except Exception:
            logger.debug("Error closing async redis streams client", exc_info=True)
        finally:
            self._redis = None


# Sync producer convenience --------------------------------------------------


def xadd_json_frame(
    stream_key: str,
    frame: dict[str, Any],
    *,
    maxlen: int = 500,
    approximate: bool = True,
) -> str | None:
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


async def initialize_async_redis_pool() -> None:
    """
    Initialize the async Redis connection pool at application startup.
    Safe to call multiple times - will only initialize once.
    """
    await _async_redis_manager.initialize()


async def close_async_redis_pool() -> None:
    """
    Close the async Redis connection pool.
    """
    await _async_redis_manager.close_pool()
