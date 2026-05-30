"""Shared helpers for direct Microsoft Graph API calls.

OAuth tokens are managed by custom OAuth (token storage + refresh via
token_resolution module). API calls go directly to Microsoft Graph.

All requests go through _request_with_rate_limit_retry() which handles
Microsoft Graph's 429 rate-limit responses with exponential backoff + jitter.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.api_error_utils import IntegrationPermissionError
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
    get_access_token,
)

logger = get_logger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com"

__all__ = [
    "IntegrationTokenInfo",
    "call_with_token_retry",
    "get_access_token",
    "graph_api_request",
    "graph_api_post_no_content",
    "graph_api_post_json",
    "graph_api_patch_json",
    "graph_api_request_url",
]

# ---------------------------------------------------------------------------
# 429 Rate-limit retry — Microsoft Graph throttles per-app and per-mailbox.
# Under concurrent sync of multiple integrations, transient 429s are expected.
# ---------------------------------------------------------------------------
_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


def _get_retry_after_seconds(response: httpx.Response) -> float | None:
    """Extract Retry-After header value in seconds, if present and valid."""
    # Honor server-provided Retry-After where possible, instead of guessing
    # backoff for every throttling response.
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

    Microsoft Graph returns 429 when per-app or per-mailbox quota is exceeded.
    Retries with exponential backoff + jitter, respecting Retry-After header
    when provided. Write callers must use Graph-side idempotency, such as an
    event transactionId, before wrapping requests here.
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

            jitter = random.uniform(0, wait * 0.5)  # noqa: S311  # nosec B311
            total_wait = wait + jitter

            logger.warning(
                "Microsoft Graph 429 rate-limited — retry %d/%d after %.1fs",
                attempt + 1,
                max_retries,
                total_wait,
            )
            await asyncio.sleep(total_wait)

    raise last_exc or RuntimeError("Exhausted retries without exception")


async def graph_api_request(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a GET request to the Microsoft Graph API with 429 rate-limit retry.

    Raises httpx.HTTPStatusError on non-2xx/non-429 responses.
    """
    # Keep request URL assembly close to this helper so every caller gets consistent
    # base URL handling and retry behavior.
    url = f"{GRAPH_BASE_URL}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.get(url, params=params)
        if response.status_code == 403:
            response_text = response.text.lower()
            if (
                "insufficient privileges" in response_text
                or "required application permissions" in response_text
                or "insufficient permissions" in response_text
            ):
                raise IntegrationPermissionError(
                    "Required Microsoft permissions were not granted. Please reconnect this integration.",
                    status_code=403,
                    provider="microsoft",
                    endpoint=endpoint,
                    response_summary=response.text,
                )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    return await _request_with_rate_limit_retry(_do)


async def graph_api_post_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST JSON to Microsoft Graph with the shared 429 retry/error handling.

    Kept separate from graph_api_request() so existing Microsoft sync callers
    remain GET-only and do not inherit new write semantics.
    """
    # POST helper is isolated to avoid accidental spread of write-path semantics
    # into older read-only sync code.
    url = f"{GRAPH_BASE_URL}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.post(url, params=params, json=json_body)
        if response.status_code == 403:
            response_text = response.text.lower()
            if (
                "insufficient privileges" in response_text
                or "required application permissions" in response_text
                or "insufficient permissions" in response_text
            ):
                raise IntegrationPermissionError(
                    "Required Microsoft permissions were not granted. Please reconnect this integration.",
                    status_code=403,
                    provider="microsoft",
                    endpoint=endpoint,
                    response_summary=response.text,
                )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    return await _request_with_rate_limit_retry(_do)


async def graph_api_post_no_content(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
    params: dict[str, Any] | None = None,
    retry_on_rate_limit: bool = False,
) -> None:
    """POST JSON to Microsoft Graph when a 2xx response has no JSON body."""
    url = f"{GRAPH_BASE_URL}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.post(url, params=params, json=json_body)
        if response.status_code == 403:
            response_text = response.text.lower()
            if (
                "insufficient privileges" in response_text
                or "required application permissions" in response_text
                or "insufficient permissions" in response_text
            ):
                raise IntegrationPermissionError(
                    "Required Microsoft permissions were not granted. Please reconnect this integration.",
                    status_code=403,
                    provider="microsoft",
                    endpoint=endpoint,
                    response_summary=response.text,
                )
        response.raise_for_status()
        return {}

    max_retries = _MAX_RETRIES if retry_on_rate_limit else 0
    await _request_with_rate_limit_retry(_do, max_retries=max_retries)
    return None


async def graph_api_patch_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """PATCH JSON to Microsoft Graph with the shared 429 retry/error handling.

    Currently used by subscription renewal. Keeping it in the provider module
    means renewal and sync calls share the same permission-error translation.
    """
    url = f"{GRAPH_BASE_URL}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.patch(url, params=params, json=json_body)
        if response.status_code == 403:
            response_text = response.text.lower()
            if (
                "insufficient privileges" in response_text
                or "required application permissions" in response_text
                or "insufficient permissions" in response_text
            ):
                raise IntegrationPermissionError(
                    "Required Microsoft permissions were not granted. "
                    "Please reconnect this integration.",
                    status_code=403,
                    provider="microsoft",
                    endpoint=endpoint,
                    response_summary=response.text,
                )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    return await _request_with_rate_limit_retry(_do)


async def graph_api_request_url(
    client: httpx.AsyncClient,
    url: str,
) -> dict[str, Any]:
    """Follow an opaque Microsoft Graph URL (@odata.nextLink or @odata.deltaLink).

    These URLs must be used as-is — Microsoft Graph encodes pagination state
    into them and any URL decode/re-encode corrupts the tokens.

    Includes 429 rate-limit retry. The URL is used as an opaque token container.
    This function must not mutate query values.
    """

    async def _do() -> dict[str, Any]:
        response = await client.get(url)
        if response.status_code == 403:
            response_text = response.text.lower()
            if (
                "insufficient privileges" in response_text
                or "required application permissions" in response_text
                or "insufficient permissions" in response_text
            ):
                raise IntegrationPermissionError(
                    "Required Microsoft permissions were not granted. Please reconnect this integration.",
                    status_code=403,
                    provider="microsoft",
                    endpoint=url,
                    response_summary=response.text,
                )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    return await _request_with_rate_limit_retry(_do)
