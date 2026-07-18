"""Rate limiting primitives for user and ingress endpoints.

This module is called by both authenticated routes and public/private ingestion
flows. It keeps a fixed-window accounting model so
teams can tune protections per path through config and keep operational behavior
predictable across endpoints.
"""

from __future__ import annotations

import time
from typing import cast

from fastapi import Depends, HTTPException, Request, status
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
)
from src.domain.models.master_tables import SuperAdmin
from src.infra.auth.admin_auth import admin_auth
from src.infra.config.configs import rate_limit_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_FORM_READ_PER_MINUTE = rate_limit_config.PUBLIC_FORM_READ_PER_MINUTE
PUBLIC_FORM_READ_PER_HOUR = rate_limit_config.PUBLIC_FORM_READ_PER_HOUR
PUBLIC_FORM_SUBMIT_PER_MINUTE = rate_limit_config.PUBLIC_FORM_SUBMIT_PER_MINUTE
PUBLIC_FORM_SUBMIT_PER_HOUR = rate_limit_config.PUBLIC_FORM_SUBMIT_PER_HOUR
PUBLIC_FORM_SUBMIT_PER_DAY = rate_limit_config.PUBLIC_FORM_SUBMIT_PER_DAY
PRIVATE_LEAD_INGESTION_PER_MINUTE = rate_limit_config.PRIVATE_LEAD_INGESTION_PER_MINUTE
PRIVATE_LEAD_INGESTION_PER_HOUR = rate_limit_config.PRIVATE_LEAD_INGESTION_PER_HOUR


def _incr_with_ttl(key: str, ttl_seconds: int) -> int:
    """Increment a Redis key and return the updated count.

    TTL is attached on first creation so fixed windows always expire even if a
    caller never sends traffic again. Without this, stale buckets can accumulate
    and hide inactive keys during incident analysis.
    """
    client = get_redis_client()
    try:
        count_raw = client.incr(key)
        count = cast(int, count_raw)
        if count == 1:
            # Fresh buckets are short-lived by default, which keeps abuse windows
            # tight and avoids retaining many zero/idle counters in Redis.
            client.expire(key, ttl_seconds)
        return count
    except Exception as e:
        # Fail-open: if Redis is unavailable, log and allow the request
        logger.warning("Rate limiter Redis error for key %s: %s", key, e)
        return 0


def _ttl_seconds(key: str) -> int | None:
    """Get TTL in seconds for a key; returns None if unknown or no TTL."""
    try:
        client = get_redis_client()
        ttl_raw = client.ttl(key)
        ttl_value = cast(int | None, ttl_raw)
        # -2: key does not exist, -1: key exists but has no associated expire
        if ttl_value is None:
            return None
        if ttl_value < 0:
            return None
        return ttl_value
    except Exception:
        return None


async def _get_email_from_request(req: Request) -> str | None:
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


def _enforce_rate_limit_for_limits(
    subject_kind: str,
    subject_id: str,
    category: str,
    *,
    per_minute_limit: int,
    per_hour_limit: int,
) -> None:
    """Core limiter logic with explicit fixed-window budgets."""
    # Use fixed buckets instead of token buckets to keep enforcement simple and
    # easy to reason about in operational incidents (how many hits landed in the
    # current minute/hour window).
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


def _enforce_daily_rate_limit_for_limit(
    subject_kind: str,
    subject_id: str,
    category: str,
    *,
    per_day_limit: int,
) -> None:
    """Core fixed-window daily limiter for anonymous write surfaces."""
    now = int(time.time())
    day_window = 86400
    day_bucket = now // day_window
    day_key = f"rl:{subject_kind}:{subject_id}:cat:{category}:d:{day_bucket}"
    day_count = _incr_with_ttl(day_key, day_window)

    if day_count and day_count > per_day_limit:
        retry_after = _ttl_seconds(day_key) or day_window
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests (daily limit exceeded)",
            headers={"Retry-After": str(retry_after)},
        )


def _enforce_rate_limit_for(subject_kind: str, subject_id: str, category: str) -> None:
    """Core limiter wrapper that chooses a budget for authenticated API subjects.

    subject_kind: short prefix like 'u' (user) or 'a' (admin) to separate key spaces.
    subject_id: identifier string for the subject to rate limit.
    """
    # Limits come from central config; allow distinct budgets per category so read
    # requests can be tuned separately from write/bulk request patterns.
    if category == "read":
        per_minute_limit = getattr(
            rate_limit_config, "READ_PER_MINUTE", rate_limit_config.PER_MINUTE
        )
        per_hour_limit = getattr(rate_limit_config, "READ_PER_HOUR", rate_limit_config.PER_HOUR)
    else:
        per_minute_limit = rate_limit_config.PER_MINUTE
        per_hour_limit = rate_limit_config.PER_HOUR

    _enforce_rate_limit_for_limits(
        subject_kind,
        subject_id,
        category,
        per_minute_limit=per_minute_limit,
        per_hour_limit=per_hour_limit,
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


def enforce_contact_research_rate_limit(user_id: str) -> None:
    """Apply the paid-provider budget to one authenticated agent user.

    Agent tools run outside FastAPI dependency injection, so callers pass the
    immutable user ID from ``AgentContext``. Cache hits must skip this function.
    """
    _enforce_rate_limit_for_limits(
        "u",
        user_id,
        "contact_research",
        per_minute_limit=rate_limit_config.CONTACT_RESEARCH_PER_MINUTE,
        per_hour_limit=rate_limit_config.CONTACT_RESEARCH_PER_HOUR,
    )


def rate_limit_admin_read(admin: SuperAdmin = Depends(admin_auth)) -> None:
    """Per-admin limiter for read endpoints (GET) in admin APIs.

    Uses lenient 'read' budget separate from org user limits.
    """
    _enforce_rate_limit_for("a", str(admin.admin_id), "read")


def client_ip_for_external_ingress(request: Request) -> str:
    """Return the client IP used for ingress-side rate limiting."""
    # Trusting only verified proxies prevents spoofed headers from resetting
    # quotas and makes quota boundaries hard to abuse from direct traffic.
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for and rate_limit_config.TRUST_EXTERNAL_PROXY_HEADERS:
        first_ip = forwarded_for.split(",", maxsplit=1)[0].strip()
        if first_ip:
            return first_ip[:128]

    return (request.client.host if request.client else "unknown")[:128]


def _public_form_subject_id(request: Request, public_form_key: str) -> str:
    return f"{public_form_key}:{client_ip_for_external_ingress(request)}"


def enforce_public_lead_form_read_rate_limit(request: Request, public_form_key: str) -> None:
    """Limit public form definition reads by form key and client IP."""
    # A single anonymous form key is scoped by IP, not by app user identity,
    # because form definitions are loaded by browser clients and bots.
    _enforce_rate_limit_for_limits(
        "public_form",
        _public_form_subject_id(request, public_form_key),
        "definition",
        per_minute_limit=PUBLIC_FORM_READ_PER_MINUTE,
        per_hour_limit=PUBLIC_FORM_READ_PER_HOUR,
    )


def enforce_public_lead_form_submit_rate_limit(request: Request, public_form_key: str) -> None:
    """Limit public form submissions by form key and client IP."""
    # Submissions are more abuse-prone than reads, so we enforce a separate
    # profile at minute, hour, and daily boundaries.
    subject_id = _public_form_subject_id(request, public_form_key)
    _enforce_rate_limit_for_limits(
        "public_form",
        subject_id,
        "submission",
        per_minute_limit=PUBLIC_FORM_SUBMIT_PER_MINUTE,
        per_hour_limit=PUBLIC_FORM_SUBMIT_PER_HOUR,
    )
    _enforce_daily_rate_limit_for_limit(
        "public_form",
        subject_id,
        "submission",
        per_day_limit=PUBLIC_FORM_SUBMIT_PER_DAY,
    )


def enforce_private_lead_ingestion_rate_limit(
    key_id: str,
    policy: dict | None = None,
) -> None:
    """Limit private lead ingestion writes by private ingress key id."""
    # Policy is optional so callers can define stricter per-key quotas without
    # changing global defaults (for example partner keys with lower abuse tolerance).
    policy = policy or {}
    per_minute_limit = int(
        policy.get("per_minute")
        or policy.get("per_minute_limit")
        or PRIVATE_LEAD_INGESTION_PER_MINUTE
    )
    per_hour_limit = int(
        policy.get("per_hour") or policy.get("per_hour_limit") or PRIVATE_LEAD_INGESTION_PER_HOUR
    )
    _enforce_rate_limit_for_limits(
        "private_lead_ingestion",
        key_id,
        "submission",
        per_minute_limit=per_minute_limit,
        per_hour_limit=per_hour_limit,
    )


async def rate_limit_login(request: Request) -> None:
    """Limit login attempts by a combination of email and client IP.

    This targets the public user login endpoint only and avoids admin APIs.
    It uses stricter budgets from config to reduce brute-force risk.

    Two-layer protection:
    1. IP+email combo limit: Prevents brute-forcing a single account
    2. IP-only total limit: Prevents credential stuffing across many emails
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

    # --- Layer 1: IP-only total limit (credential stuffing protection) ---
    ip_total_minute_key = f"rl:login:ip_total:{ip}:m:{minute_bucket}"
    ip_total_hour_key = f"rl:login:ip_total:{ip}:h:{hour_bucket}"

    ip_total_minute_count = _incr_with_ttl(ip_total_minute_key, minute_window)
    ip_total_hour_count = _incr_with_ttl(ip_total_hour_key, hour_window)

    ip_total_per_min = getattr(rate_limit_config, "LOGIN_IP_TOTAL_PER_MINUTE", 100)
    ip_total_per_hr = getattr(rate_limit_config, "LOGIN_IP_TOTAL_PER_HOUR", 300)

    if ip_total_minute_count and ip_total_minute_count > ip_total_per_min:
        retry_after = _ttl_seconds(ip_total_minute_key) or minute_window
        logger.warning(f"Login IP total rate limit exceeded (per-minute) for ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts from this location. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if ip_total_hour_count and ip_total_hour_count > ip_total_per_hr:
        retry_after = _ttl_seconds(ip_total_hour_key) or hour_window
        logger.warning(f"Login IP total rate limit exceeded (per-hour) for ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts from this location. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    # --- Layer 2: IP+email combo limit (brute-force protection per account) ---
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


async def rate_limit_admin_login(request: Request) -> None:
    """Limit admin login attempts by a combination of email and client IP.

    This targets admin login endpoints with stricter limits than regular user login
    to protect privileged admin accounts from brute-force attacks.

    Two-layer protection:
    1. IP-only total limit: Prevents credential stuffing across many admin emails
    2. IP+email combo limit: Prevents brute-forcing a single admin account
    """
    # Identify admin email from request body
    email = await _get_email_from_request(request)

    # Client IP (behind proxy you may want X-Forwarded-For)
    ip = request.client.host if request.client else "unknown"

    now = int(time.time())
    minute_window = 60
    hour_window = 3600
    minute_bucket = now // minute_window
    hour_bucket = now // hour_window

    # --- Layer 1: IP-only total limit (credential stuffing protection) ---
    # Admin login uses stricter IP-only limits (20/min, 50/hr)
    ip_total_minute_key = f"rl:admin_login:ip_total:{ip}:m:{minute_bucket}"
    ip_total_hour_key = f"rl:admin_login:ip_total:{ip}:h:{hour_bucket}"

    ip_total_minute_count = _incr_with_ttl(ip_total_minute_key, minute_window)
    ip_total_hour_count = _incr_with_ttl(ip_total_hour_key, hour_window)

    # Stricter IP total limits for admin (20/min, 50/hr vs 100/300 for user)
    ip_total_per_min = 20
    ip_total_per_hr = 50

    if ip_total_minute_count and ip_total_minute_count > ip_total_per_min:
        retry_after = _ttl_seconds(ip_total_minute_key) or minute_window
        logger.warning(f"Admin login IP total rate limit exceeded (per-minute) for ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many admin login attempts from this location. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if ip_total_hour_count and ip_total_hour_count > ip_total_per_hr:
        retry_after = _ttl_seconds(ip_total_hour_key) or hour_window
        logger.warning(f"Admin login IP total rate limit exceeded (per-hour) for ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many admin login attempts from this location. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    # --- Layer 2: IP+email combo limit (brute-force protection per account) ---
    base = f"rl:admin_login:ip:{ip}"
    if email:
        base += f":email:{email.lower()}"

    minute_key = f"{base}:m:{minute_bucket}"
    hour_key = f"{base}:h:{hour_bucket}"

    minute_count = _incr_with_ttl(minute_key, minute_window)
    hour_count = _incr_with_ttl(hour_key, hour_window)

    # Stricter limits for admin login (3 per minute, 20 per hour)
    per_min = getattr(rate_limit_config, "ADMIN_LOGIN_PER_MINUTE", 3)
    per_hr = getattr(rate_limit_config, "ADMIN_LOGIN_PER_HOUR", 20)

    if minute_count and minute_count > per_min:
        retry_after = _ttl_seconds(minute_key) or minute_window
        logger.warning(f"Admin login rate limit exceeded (per-minute) for email={email}, ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many admin login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if hour_count and hour_count > per_hr:
        retry_after = _ttl_seconds(hour_key) or hour_window
        logger.warning(f"Admin login rate limit exceeded (per-hour) for email={email}, ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many admin login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


async def rate_limit_password_reset(request: Request) -> None:
    """Limit password reset requests to prevent abuse and email bombing.

    Applies to both user and admin forgot-password endpoints.
    Uses IP-based rate limiting (3 per 15 minutes, 10 per hour).
    """
    # Client IP (behind proxy you may want X-Forwarded-For)
    ip = request.client.host if request.client else "unknown"

    # If email is present, combine IP+email for brute-force protection per
    # account while still falling back to IP-only limits when unknown to avoid
    # over-restricting unauthenticated or malformed callers.
    email = await _get_email_from_request(request)

    now = int(time.time())
    fifteen_min_window = 900  # 15 minutes
    hour_window = 3600
    fifteen_min_bucket = now // fifteen_min_window
    hour_bucket = now // hour_window

    base = f"rl:pwd_reset:ip:{ip}"
    if email:
        base += f":email:{email.lower()}"

    fifteen_min_key = f"{base}:15m:{fifteen_min_bucket}"
    hour_key = f"{base}:h:{hour_bucket}"

    fifteen_min_count = _incr_with_ttl(fifteen_min_key, fifteen_min_window)
    hour_count = _incr_with_ttl(hour_key, hour_window)

    # Conservative limits: 3 per 15 min, 10 per hour
    per_15min = getattr(rate_limit_config, "PASSWORD_RESET_PER_15MIN", 3)
    per_hr = getattr(rate_limit_config, "PASSWORD_RESET_PER_HOUR", 10)

    if fifteen_min_count and fifteen_min_count > per_15min:
        retry_after = _ttl_seconds(fifteen_min_key) or fifteen_min_window
        logger.warning(f"Password reset rate limit exceeded (15-min) for email={email}, ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password reset requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if hour_count and hour_count > per_hr:
        retry_after = _ttl_seconds(hour_key) or hour_window
        logger.warning(f"Password reset rate limit exceeded (hourly) for email={email}, ip={ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password reset requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


__all__ = [
    "rate_limit_user_request",
    "rate_limit_user_write",
    "rate_limit_login",
    "rate_limit_admin_login",
    "rate_limit_password_reset",
    "rate_limit_user_read",
    "enforce_contact_research_rate_limit",
    "rate_limit_admin_read",
    "enforce_public_lead_form_read_rate_limit",
    "enforce_public_lead_form_submit_rate_limit",
    "client_ip_for_external_ingress",
    "enforce_private_lead_ingestion_rate_limit",
]
