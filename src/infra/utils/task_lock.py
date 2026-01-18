"""Redis-based distributed task lock for Celery task deduplication.

This module provides a simple mechanism to prevent concurrent execution
of the same task for the same resource (e.g., organisation_id).

Usage:
    from src.infra.utils.task_lock import acquire_task_lock, release_task_lock

    # In a Celery task
    lock_key = f"task_lock:my_task:{org_id}"
    if not acquire_task_lock(lock_key, ttl=600):
        logger.info("Task already running, skipping")
        return {"status": "skipped", "reason": "already_running"}

    try:
        # Do work...
    finally:
        release_task_lock(lock_key)
"""

from __future__ import annotations

from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def acquire_task_lock(lock_key: str, ttl: int = 600) -> bool:
    """Acquire a distributed lock for a task.

    Uses Redis SET NX (set if not exists) with expiration.

    Args:
        lock_key: Unique key identifying the task + resource (e.g., "task:match_calendar:org_id")
        ttl: Lock time-to-live in seconds. Should be >= task timeout.
             Default 600 (10 minutes).

    Returns:
        True if lock acquired, False if already held by another process.
    """
    try:
        client = get_redis_client()
        # SET NX returns True if key was set, False if it already exists
        acquired = client.set(lock_key, "1", nx=True, ex=ttl)
        if acquired:
            logger.debug("Acquired task lock: %s (ttl=%ds)", lock_key, ttl)
        else:
            logger.info("Task lock already held: %s", lock_key)
        return bool(acquired)
    except Exception as e:
        # If Redis is unavailable, allow the task to proceed (fail-open)
        logger.warning("Failed to acquire task lock %s: %s", lock_key, e)
        return True


def release_task_lock(lock_key: str) -> bool:
    """Release a distributed lock.

    Args:
        lock_key: The lock key to release.

    Returns:
        True if lock was released, False if it didn't exist or error occurred.
    """
    try:
        client = get_redis_client()
        deleted = client.delete(lock_key)
        if deleted:
            logger.debug("Released task lock: %s", lock_key)
        return bool(deleted)
    except Exception as e:
        logger.warning("Failed to release task lock %s: %s", lock_key, e)
        return False


def is_task_locked(lock_key: str) -> bool:
    """Check if a task lock is currently held.

    Args:
        lock_key: The lock key to check.

    Returns:
        True if lock exists, False otherwise.
    """
    try:
        client = get_redis_client()
        return bool(client.exists(lock_key))
    except Exception as e:
        logger.warning("Failed to check task lock %s: %s", lock_key, e)
        return False
