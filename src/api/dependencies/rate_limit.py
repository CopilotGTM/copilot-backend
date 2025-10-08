"""Per-user rate limiting dependency using Redis.

Applies simple fixed-window counters for 1-minute and 1-hour windows.
Defaults are conservative for testing and can be overridden via env vars:
 - RATE_LIMIT_PER_MINUTE (default: 5)
 - RATE_LIMIT_PER_HOUR (default: 5)

Usage: include as a router dependency on user-protected endpoints.
"""

from __future__ import annotations
import time
from typing import Optional

from fastapi import Depends, HTTPException, status

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
)
from fastapi import Request
from src.infra.auth.admin_auth import admin_auth
from src.domain.models.master_tables import SuperAdmin
from src.infra.messaging.redis_client import get_redis_client
from src.infra.config.configs import rate_limit_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def _incr_with_ttl(key: str, ttl_seconds: int) -> int:
    """Increment a Redis key and set TTL on first hit.

    Returns the incremented count.
    """
    client = get_redis_client()
    try:
        count = client.incr(key)
        if count == 1:
            # Set TTL only on first creation
            client.expire(key, ttl_seconds)
        return int(count)
    except Exception as e:
        # Fail-open: if Redis is unavailable, log and allow the request
        logger.warning("Rate limiter Redis error for key %s: %s", key, e)
        return 0


def _ttl_seconds(key: str) -> Optional[int]:
    """Get TTL in seconds for a key; returns None if unknown or no TTL."""
    try:
        client = get_redis_client()
        ttl = client.ttl(key)
        # -2: key does not exist, -1: key exists but has no associated expire
        if ttl is None or ttl < 0:
            return None
        return int(ttl)
    except Exception:
        return None


async def _get_email_from_request(req: Request) -> Optional[str]:
    """Best-effort extraction of email/username from the request body or headers."""
    # Try JSON body
    try:
        body_bytes = await req.body()
        if body_bytes:
            import json

            try:
                data = json.loads(body_bytes)
                if isinstance(data, dict):
                    val = data.get("email") or data.get("username")
                    if isinstance(val, str) and val:
                        return val
            except Exception:
                pass
    except Exception:
        pass
    # Header fallback
    val = req.headers.get("X-Auth-Email")
    if isinstance(val, str) and val:
        return val
    # Query fallback for dev tools
    val = req.query_params.get("email")
    if isinstance(val, str) and val:
        return val
    return None


def _enforce_rate_limit_for(subject_kind: str, subject_id: str, category: str) -> None:
    """Core limiter logic with simple fixed windows and optional category.

    subject_kind: short prefix like 'u' (user) or 'a' (admin) to separate key spaces.
    subject_id: identifier string for the subject to rate limit.
    """
    # Limits come from central config; allow distinct budgets per category
    if category == "read":
        per_minute_limit = getattr(
            rate_limit_config, "READ_PER_MINUTE", rate_limit_config.PER_MINUTE
        )
        per_hour_limit = getattr(
            rate_limit_config, "READ_PER_HOUR", rate_limit_config.PER_HOUR
        )
    else:
        per_minute_limit = rate_limit_config.PER_MINUTE
        per_hour_limit = rate_limit_config.PER_HOUR

    now = int(time.time())
    minute_window = 60
    hour_window = 3600

    minute_bucket = now // minute_window
    hour_bucket = now // hour_window

    # Keys scoped per subject and category to allow distinct budgets (e.g., write/read)
    minute_key = f"rl:{subject_kind}:{subject_id}:cat:{category}:m:{minute_bucket}"
    hour_key = f"rl:{subject_kind}:{subject_id}:cat:{category}:h:{hour_bucket}"

    minute_count = _incr_with_ttl(minute_key, minute_window)
    hour_count = _incr_with_ttl(hour_key, hour_window)

    if minute_count and minute_count > per_minute_limit:
        retry_after = _ttl_seconds(minute_key) or minute_window
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests (per-minute limit exceeded)",
            headers={"Retry-After": str(retry_after)},
        )

    if hour_count and hour_count > per_hour_limit:
        retry_after = _ttl_seconds(hour_key) or hour_window
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests (per-hour limit exceeded)",
            headers={"Retry-After": str(retry_after)},
        )


def _enforce_rate_limit(user: UserIDs, category: str = "global") -> None:
    """Backward-compatible wrapper for org user rate limiting."""
    _enforce_rate_limit_for("u", str(user.user_id), category)


def rate_limit_user_request(user: UserIDs = Depends(get_current_user)) -> None:
    """Generic per-user limiter (global category)."""
    _enforce_rate_limit(user, category="global")


def rate_limit_user_write(user: UserIDs = Depends(get_current_user)) -> None:
    """Per-user limiter for write endpoints (POST/PATCH/DELETE).

    Uses a shared 'write' budget across all write endpoints to prevent
    users from bypassing limits by hopping between routes.
    """
    _enforce_rate_limit(user, category="write")


def rate_limit_user_read(user: UserIDs = Depends(get_current_user)) -> None:
    """Per-user limiter for read endpoints (GET).

    Uses a lenient 'read' budget to throttle abusive patterns while
    minimizing impact on normal browsing.
    """
    _enforce_rate_limit(user, category="read")


def rate_limit_admin_read(admin: SuperAdmin = Depends(admin_auth)) -> None:
    """Per-admin limiter for read endpoints (GET) in admin APIs.

    Uses lenient 'read' budget separate from org user limits.
    """
    _enforce_rate_limit_for("a", str(admin.admin_id), "read")


async def rate_limit_login(request: Request) -> None:
    """Limit login attempts by a combination of email and client IP.

    This targets the public user login endpoint only and avoids admin APIs.
    It uses stricter budgets from config to reduce brute-force risk.
    """
    # Identify user/email; dependencies that read the body later still work (Starlette caches body)
    email = await _get_email_from_request(request)

    # Client IP (behind proxy you may want X-Forwarded-For)
    ip = request.client.host if request.client else "unknown"

    now = int(time.time())
    minute_window = 60
    hour_window = 3600
    minute_bucket = now // minute_window
    hour_bucket = now // hour_window

    base = f"rl:login:ip:{ip}"
    if email:
        base += f":email:{email.lower()}"

    minute_key = f"{base}:m:{minute_bucket}"
    hour_key = f"{base}:h:{hour_bucket}"

    minute_count = _incr_with_ttl(minute_key, minute_window)
    hour_count = _incr_with_ttl(hour_key, hour_window)

    per_min = getattr(rate_limit_config, "AUTH_LOGIN_PER_MINUTE", 10)
    per_hr = getattr(rate_limit_config, "AUTH_LOGIN_PER_HOUR", 50)

    if minute_count and minute_count > per_min:
        retry_after = _ttl_seconds(minute_key) or minute_window
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if hour_count and hour_count > per_hr:
        retry_after = _ttl_seconds(hour_key) or hour_window
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


__all__ = [
    "rate_limit_user_request",
    "rate_limit_user_write",
    "rate_limit_login",
    "rate_limit_user_read",
    "rate_limit_admin_read",
]
