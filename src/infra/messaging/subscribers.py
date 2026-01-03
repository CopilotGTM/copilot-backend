"""Centralized Redis subscriber abstractions (sync + async)."""

import json

from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger


class RedisSubscriber:
    """Synchronous Redis pub/sub subscriber."""

    def __init__(self, logger=None):
        self.logger = logger if logger else get_logger("redis_subscriber")

    def subscribe(self, channel, handler):
        client = get_redis_client()
        pubsub = client.pubsub()
        pubsub.subscribe(channel)
        self.logger.info(f"Subscribed to Redis channel: {channel}")
        try:
            for msg in pubsub.listen():
                if msg["type"] == "message":
                    try:
                        data = json.loads(msg["data"])
                        handler(data)
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to parse or handle message from {channel}: {e}"
                        )
        except Exception as e:
            self.logger.error(f"Error in RedisSubscriber.listen for {channel}: {e}")
        finally:
            pubsub.unsubscribe(channel)
            pubsub.close()


# Async stream/pubsub variants

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None


class AsyncRedisSubscriber:
    """Async Redis pub/sub or stream subscriber."""

    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger if logger else get_logger("redis_async_subscriber")
        self.redis = None
        self.pubsub = None

    async def _get_redis_client(self):
        if not aioredis:
            raise RuntimeError("Async redis support not available. Install redis>=6.0.0")
        if self.redis is None:
            redis_host, port, password = self.config.get_redis_client_details

            # Determine if SSL should be used (disabled for local hosts and Docker containers)
            ssl_enabled = not (
                redis_host in ["localhost", "127.0.0.1", "redis"]
                or redis_host.startswith("192.168.")
                or self.config.env == "local"
            )

            # Use rediss:// protocol for SSL, redis:// for non-SSL
            protocol = "rediss" if ssl_enabled else "redis"
            redis_url = f"{protocol}://:{password}@{redis_host}:{port}"

            self.redis = aioredis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=None,
                socket_connect_timeout=30,
                retry_on_timeout=True,
                health_check_interval=0,
                socket_keepalive=True,
            )
            await self.redis.ping()
        return self.redis

    async def subscribe(self, channel, handler):
        redis_client = await self._get_redis_client()
        self.pubsub = redis_client.pubsub()
        await self.pubsub.subscribe(channel)
        self.logger.info(f"Subscribed to Redis channel (async): {channel}")
        try:
            async for msg in self.pubsub.listen():
                if msg["type"] == "message":
                    try:
                        data = json.loads(msg["data"])
                        await handler(data)
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to parse or handle async message from {channel}: {e}"
                        )
        except Exception as e:
            self.logger.error(f"Error in AsyncRedisSubscriber.listen for {channel}: {e}")
        finally:
            await self.pubsub.unsubscribe(channel)
            await self.pubsub.close()
            self.pubsub = None
