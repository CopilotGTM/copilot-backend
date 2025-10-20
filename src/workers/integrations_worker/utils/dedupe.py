from __future__ import annotations

from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def acquire_sync_lock(integration_id: str, data_type: str, ttl_seconds: int = 120) -> bool:
    """Acquire a short-lived Redis lock to dedupe bursty webhooks.

    Returns True if lock acquired, False otherwise.
    """
    try:
        redis = get_redis_client()
        key = f"sync:lock:{integration_id}:{data_type}"
        return bool(redis.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception as e:
        logger.warning("Redis lock unavailable: %s", str(e))
        # Fail-open to keep sync running
        return True
