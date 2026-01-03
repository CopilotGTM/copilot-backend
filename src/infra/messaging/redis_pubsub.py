"""Async Redis pub/sub utilities for real-time messaging.

Provides async Redis pub/sub functionality using redis-py's async support.
Handles connection management, subscription lifecycle, and message parsing.

Fix notes:
    - Previous version checked `redis.asyncio` attribute on the imported
        submodule which itself is `redis.asyncio`, causing AttributeError.
    - We now treat absence of the async submodule as lack of support and
        raise a clear runtime error.
"""

import json
from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as redis
from src.infra.config.configs import Config
from src.infra.utils.logger import get_logger

logger = get_logger("redis_pubsub")


class AsyncRedisPubSub:
    """Async Redis pub/sub client with connection management."""

    def __init__(self):
        self.config = Config()
        self._redis: Any | None = None
        self._pubsub: Any | None = None

    async def _get_redis_client(self) -> Any:
        """Get or create async Redis client."""
        if self._redis is None:
            if redis is None:
                raise RuntimeError(
                    "Async redis support not available. Install/upgrade with: pip install 'redis>=6.0.0'"
                )

            redis_host, redis_port, redis_password = self.config.get_redis_client_details

            # Determine if SSL should be used (disabled for local hosts and Docker containers)
            ssl_enabled = not (
                redis_host in ["localhost", "127.0.0.1", "redis"]
                or redis_host.startswith("192.168.")
                or self.config.env == "local"
            )

            # Use rediss:// protocol for SSL, redis:// for non-SSL
            protocol = "rediss" if ssl_enabled else "redis"
            redis_url = f"{protocol}://:{redis_password}@{redis_host}:{redis_port}"

            # For long‑lived Pub/Sub, avoid read timeouts and client‑side health checks that
            # inject PING (which can conflict with pubsub protocol). OS TCP keepalive is enabled.
            self._redis = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=None,  # no read timeout for blocking pubsub
                socket_connect_timeout=30,
                retry_on_timeout=True,
                health_check_interval=0,  # disable client PING health check
                socket_keepalive=True,
            )
            await self._redis.ping()  # connectivity check
        return self._redis

    async def subscribe_to_channel(self, channel: str) -> AsyncGenerator[dict[str, Any], None]:
        """Subscribe to a Redis channel and yield messages.

        Args:
            channel: Redis channel name to subscribe to

        Yields:
            Parsed message data as dict
        """
        try:
            redis_client = await self._get_redis_client()
            self._pubsub = redis_client.pubsub()
            await self._pubsub.subscribe(channel)

            logger.info(f"Subscribed to Redis channel: {channel}")

            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        yield data
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Failed to parse message from {channel}: {e}")
                        continue

        except Exception as e:
            logger.error(f"Redis pub/sub error for channel {channel}: {e}")
            raise

    async def subscribe_and_poll(
        self, channel: str, poll_timeout: float = 1.0
    ) -> AsyncGenerator[dict[str, Any] | None, None]:
        """Subscribe to a Redis channel and poll for messages with a timeout.

        This is intended for SSE-style consumers that need to perform periodic work
        (e.g., emitting heartbeats) even when no messages arrive.

        Yields:
            - dict: parsed message payload when a message is received
            - None: when no message is received within poll_timeout
        """
        try:
            redis_client = await self._get_redis_client()
            self._pubsub = redis_client.pubsub()
            await self._pubsub.subscribe(channel)

            logger.info(f"Subscribed to Redis channel (polling): {channel}")

            while True:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=poll_timeout,
                )

                if message is None:
                    yield None
                    continue

                if message.get("type") != "message":
                    continue

                try:
                    data = json.loads(message["data"])
                    yield data
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"Failed to parse message from {channel}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Redis pub/sub polling error for channel {channel}: {e}")
            raise

    async def publish_message(self, channel: str, data: dict[str, Any]) -> None:
        """Publish a message to a Redis channel.

        Args:
            channel: Redis channel name
            data: Message data to publish
        """
        try:
            redis_client = await self._get_redis_client()
            message = json.dumps(data)
            await redis_client.publish(channel, message)
        except Exception as e:
            logger.error(f"Failed to publish to {channel}: {e}")
            raise

    async def close(self) -> None:
        """Clean up Redis connections."""
        try:
            if self._pubsub:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
                self._pubsub = None

            if self._redis:
                await self._redis.close()
                self._redis = None

        except Exception as e:
            logger.warning(f"Error closing Redis connections: {e}")
