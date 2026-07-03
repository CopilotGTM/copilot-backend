"""Redis sorted set scheduling layer for user reminders.

DB remains the source of truth; this ZSET is a disposable timing index.
On Redis restart, the manual recovery_sweep task can re-populate it from the DB.

Uses the existing connection pool from redis_client.get_redis_client().
"""

from __future__ import annotations

import logging
from uuid import UUID

from src.infra.messaging.redis_client import get_redis_client

logger = logging.getLogger(__name__)

REMINDER_ZSET_KEY = "reminders:due"


def schedule_reminder(org_id: UUID, reminder_id: UUID, trigger_epoch: float) -> None:
    """ZADD a reminder into the sorted set (score = trigger UTC epoch)."""
    client = get_redis_client()
    member = f"{org_id}:{reminder_id}"
    client.zadd(REMINDER_ZSET_KEY, {member: trigger_epoch})


def unschedule_reminder(org_id: UUID, reminder_id: UUID) -> None:
    """ZREM a reminder from the sorted set."""
    client = get_redis_client()
    member = f"{org_id}:{reminder_id}"
    client.zrem(REMINDER_ZSET_KEY, member)


def pop_due_reminders(now_epoch: float, limit: int = 50) -> list[tuple[str, str]]:
    """Atomically find and claim due reminders.

    Returns list of (org_id, reminder_id) tuples for reminders whose score
    (trigger epoch) is <= now_epoch. Uses ZREM as an atomic claim — if another
    worker already removed a member, ZREM returns 0 and we skip it.
    """
    client = get_redis_client()
    members: list[str] = list(
        client.zrangebyscore(  # type: ignore[arg-type]
            REMINDER_ZSET_KEY, "-inf", now_epoch, start=0, num=limit
        )
    )
    claimed: list[tuple[str, str]] = []
    for member in members:
        # ZREM returns 1 if we removed it (we own it), 0 if someone else did
        if client.zrem(REMINDER_ZSET_KEY, member):
            org_id, reminder_id = member.split(":", 1)
            claimed.append((org_id, reminder_id))
    return claimed
