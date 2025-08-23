"""Async Redis pub/sub utilities for real-time messaging.

Provides async Redis pub/sub functionality using redis-py's async support.
Handles connection management, subscription lifecycle, and message parsing.

Fix notes:
    - Previous version checked `redis.asyncio` attribute on the imported
        submodule which itself is `redis.asyncio`, causing AttributeError.
    - We now treat absence of the async submodule as lack of support and
        raise a clear runtime error.
"""
import asyncio
import json

try:  # redis >= 4.2 provides redis.asyncio; >=5 recommended
        import redis.asyncio as redis  # type: ignore
except ImportError:  # pragma: no cover - fallback path
        redis = None  # type: ignore

from typing import AsyncGenerator, Dict, Any, Optional
from uuid import UUID

from src.infra.config.configs import Config
from src.infra.utils.logger import get_logger

logger = get_logger("redis_pubsub")


class AsyncRedisPubSub:
    """Async Redis pub/sub client with connection management."""
    
    def __init__(self):
        self.config = Config()
        self._redis: Optional[Any] = None
        self._pubsub: Optional[Any] = None
    
    async def _get_redis_client(self) -> Any:
        """Get or create async Redis client."""
        if self._redis is None:
            if redis is None:
                raise RuntimeError("Async redis support not available. Install/upgrade with: pip install 'redis>=5.0.0'")

            redis_host, redis_port, redis_password = self.config.get_redis_client_details
            ssl_enabled = False if self.config.env == "local" else True

            self._redis = redis.Redis(  # type: ignore[attr-defined]
                host=redis_host,
                port=redis_port,
                password=redis_password,
                decode_responses=True,
                socket_timeout=30,
                socket_connect_timeout=30,
                retry_on_timeout=True,
                health_check_interval=30,
                ssl=ssl_enabled,
                socket_keepalive=True,
            )
            await self._redis.ping()  # connectivity check
        return self._redis
    
    async def subscribe_to_channel(self, channel: str) -> AsyncGenerator[Dict[str, Any], None]:
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
    
    async def publish_message(self, channel: str, data: Dict[str, Any]) -> None:
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


CHANNEL_TEMPLATE = "kb:{org_id}:sources"


def channel_for_knowledge_update_status(organisation_id: UUID | str) -> str:
    """Generate Redis channel name for organisation knowledge sources."""
    return CHANNEL_TEMPLATE.format(org_id=organisation_id)


# Backwards compatibility alias (older code imported channel_for_org)
def channel_for_org(organisation_id: UUID | str) -> str:  # pragma: no cover
    return channel_for_knowledge_update_status(organisation_id)


async def publish_source_status_async(
    organisation_id: UUID | str,
    source_id: UUID | str,
    status: str,
    updated_at: Any = None,
    error_message: Optional[str] = None,
    **extra_fields
) -> None:
    """Async version of source status publishing.
    
    Args:
        organisation_id: Organisation UUID
        source_id: Source UUID  
        status: New status value
        updated_at: Timestamp (will be converted to ISO format)
        error_message: Optional error message
        **extra_fields: Additional fields to include in message
    """
    pubsub = AsyncRedisPubSub()
    try:
        channel = channel_for_knowledge_update_status(organisation_id)
        payload = {
            "source_id": str(source_id),
            "status": status,
            "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
            "error_message": error_message,
            **extra_fields
        }
        await pubsub.publish_message(channel, payload)
    except Exception as e:
        logger.warning(f"Failed to publish source status for {source_id}: {e}")
    finally:
        await pubsub.close()
