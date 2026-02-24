"""Redis-based nonce utility for one-time-use confirmation cards.

Generates a UUID4 nonce, stores it in Redis with a TTL, and provides
atomic validate-and-consume via DELETE (returns 1 if key existed).

Used by create_deal and add_deal_note tools to prevent duplicate
submissions from stale Adaptive Cards.
"""

from __future__ import annotations

import logging
import uuid

from src.infra.messaging.redis_client import get_redis_client

logger = logging.getLogger(__name__)


def create_nonce(prefix: str, ttl: int = 900) -> str:
    """Generate a UUID4 nonce and store it in Redis.

    Args:
        prefix: Key prefix (e.g. "deal_confirm", "note_confirm")
        ttl: Time-to-live in seconds (default 15 minutes)

    Returns:
        The nonce string (UUID4)
    """
    nonce = str(uuid.uuid4())
    key = f"{prefix}:{nonce}"
    client = get_redis_client()
    client.set(key, "1", ex=ttl)
    logger.debug("Created nonce %s (ttl=%ds)", key, ttl)
    return nonce


def validate_and_consume_nonce(prefix: str, nonce: str) -> bool:
    """Atomically validate and consume a nonce (one-time use).

    Uses DELETE which returns 1 if the key existed (valid + consumed)
    or 0 if it didn't (expired/already consumed).

    Args:
        prefix: Same prefix used in create_nonce
        nonce: The nonce string to validate

    Returns:
        True if nonce was valid and consumed, False otherwise
    """
    key = f"{prefix}:{nonce}"
    try:
        client = get_redis_client()
        deleted = client.delete(key)
        if deleted:
            logger.debug("Consumed nonce %s", key)
            return True
        logger.info("Nonce rejected (expired or already used): %s", key)
        return False
    except Exception:
        logger.warning("Redis error validating nonce %s", key, exc_info=True)
        return False
