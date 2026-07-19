"""Framework-neutral Redis fixed-window rate limiting.

API dependencies and background-worker tools share these primitives. This
module must remain below the FastAPI boundary because lightweight worker images
intentionally exclude ``src.api``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import cast

from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RateLimitExceededError(Exception):
    """Describe an exhausted fixed-window budget without choosing a transport response."""

    retry_after: int
    window: str


def _incr_with_ttl(key: str, ttl_seconds: int) -> int:
    """Increment a Redis bucket and attach its expiry on first use."""
    client = get_redis_client()
    try:
        count_raw = client.incr(key)
        count = cast(int, count_raw)
        if count == 1:
            client.expire(key, ttl_seconds)
        return count
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Rate limiting fails open so a Redis incident cannot block every API
        # route and worker tool that shares this operational dependency.
        logger.warning("Rate limiter Redis error for key %s: %s", key, exc)
        return 0


def _ttl_seconds(key: str) -> int | None:
    """Return a bucket's remaining TTL when Redis can provide one."""
    try:
        ttl_raw = get_redis_client().ttl(key)
        ttl_value = cast(int | None, ttl_raw)
        return ttl_value if ttl_value is not None and ttl_value >= 0 else None
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def enforce_fixed_window_rate_limit(
    subject_kind: str,
    subject_id: str,
    category: str,
    *,
    per_minute_limit: int,
    per_hour_limit: int,
) -> None:
    """Enforce Redis-backed minute/hour limits without an HTTP dependency."""
    now = int(time.time())
    minute_window = 60
    hour_window = 3600
    minute_key = f"rl:{subject_kind}:{subject_id}:cat:{category}:m:{now // minute_window}"
    hour_key = f"rl:{subject_kind}:{subject_id}:cat:{category}:h:{now // hour_window}"

    minute_count = _incr_with_ttl(minute_key, minute_window)
    hour_count = _incr_with_ttl(hour_key, hour_window)

    if minute_count and minute_count > per_minute_limit:
        raise RateLimitExceededError(
            retry_after=_ttl_seconds(minute_key) or minute_window,
            window="minute",
        )
    if hour_count and hour_count > per_hour_limit:
        raise RateLimitExceededError(
            retry_after=_ttl_seconds(hour_key) or hour_window,
            window="hour",
        )


__all__ = ["RateLimitExceededError", "enforce_fixed_window_rate_limit"]
