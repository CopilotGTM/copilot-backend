"""Google-specific API request helpers.

Provides thin wrappers for Gmail and Google Calendar REST endpoints.
Token management is handled by the provider-agnostic token_resolution module.

All requests go through _request_with_rate_limit_retry() which handles
Google's 429 rate-limit responses with exponential backoff + jitter.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

GMAIL_BASE_URL = "https://gmail.googleapis.com"
GOOGLE_CALENDAR_BASE_URL = "https://www.googleapis.com"

# Re-export token resolution utilities so existing callers don't break
# during the migration. New code should import directly from token_resolution.
from src.service.integrations.providers.token_resolution import (  # noqa: E402
    IntegrationTokenInfo,
    call_with_token_retry,
    get_access_token,
)

__all__ = [
    "IntegrationTokenInfo",
    "call_with_token_retry",
    "get_access_token",
    "gmail_api_request",
    "calendar_api_request",
]

# ---------------------------------------------------------------------------
# 429 Rate-limit retry — Google API quotas (Gmail: 250 units/sec,
# Calendar: 500 requests/100sec/user). Under concurrent sync of multiple
# integrations, transient 429s are expected and recoverable.
# ---------------------------------------------------------------------------
_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


def _get_retry_after_seconds(response: httpx.Response) -> float | None:
    """Extract Retry-After header value in seconds, if present and valid."""
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


async def _request_with_rate_limit_retry(
    request_fn: Any,
    *,
    max_retries: int = _MAX_RETRIES,
) -> dict[str, Any]:
    """Execute an HTTP request with automatic 429 retry and exponential backoff.

    Google APIs return 429 when per-user or per-project quota is exceeded.
    Retries with exponential backoff + jitter, respecting Retry-After header
    when provided. Safe to wrap any idempotent GET request.

    Args:
        request_fn: Async callable that performs the HTTP request and returns
            the parsed JSON response. Must raise httpx.HTTPStatusError on failure.
        max_retries: Maximum number of retry attempts after the initial request.

    Returns:
        Parsed JSON response dict from the successful request.

    Raises:
        httpx.HTTPStatusError: On non-429 errors, or after exhausting all retries.
    """
    last_exc: httpx.HTTPStatusError | None = None

    for attempt in range(max_retries + 1):
        try:
            return await request_fn()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or attempt >= max_retries:
                raise

            last_exc = exc
            retry_after = _get_retry_after_seconds(exc.response)
            if retry_after is not None:
                wait = min(retry_after, _MAX_BACKOFF_SEC)
            else:
                wait = min(_BASE_BACKOFF_SEC * (2**attempt), _MAX_BACKOFF_SEC)

            # Jitter prevents thundering herd when multiple integrations
            # sync concurrently and hit quota limits simultaneously
            jitter = random.uniform(0, wait * 0.5)  # noqa: S311  # nosec B311
            total_wait = wait + jitter

            logger.warning(
                "Google API 429 rate-limited — retry %d/%d after %.1fs",
                attempt + 1,
                max_retries,
                total_wait,
            )
            await asyncio.sleep(total_wait)

    # Unreachable in normal flow, but satisfies the type checker
    raise last_exc or RuntimeError("Exhausted retries without exception")


async def gmail_api_request(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a GET request to the Gmail API with 429 rate-limit retry."""
    url = f"{GMAIL_BASE_URL}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    result: dict[str, Any] = await _request_with_rate_limit_retry(_do)
    return result


async def calendar_api_request(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a GET request to the Google Calendar API with 429 rate-limit retry."""
    url = f"{GOOGLE_CALENDAR_BASE_URL}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    result: dict[str, Any] = await _request_with_rate_limit_retry(_do)
    return result
