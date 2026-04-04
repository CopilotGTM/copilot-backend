"""Shared Microsoft Graph delta sync utilities.

Extracts the pagination loop, delta-link expiry detection, and @removed handling
that was duplicated across outlook_calendar, outlook_email, and teams providers.

Used by any provider that fetches data via Microsoft Graph delta queries
(calendarView/delta, messages/delta, chatMessage/delta).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_request,
    graph_api_request_url,
)

logger = get_logger(__name__)

# Error codes Microsoft returns when a stored deltaLink is no longer valid.
_EXPIRED_DELTA_ERROR_CODES = frozenset({"syncstatenotfound", "resyncresquired", "invalidrequest"})

# Substrings in error messages that indicate delta expiry.
_EXPIRED_DELTA_SUBSTRINGS = ("410", "gone", "sync state")


@dataclass
class DeltaSyncResult:
    """Result from a Microsoft Graph delta pagination loop."""

    items: list[dict[str, Any]] = field(default_factory=list)
    delta_link: str | None = None


class DeltaExpiredError(Exception):
    """Raised when a stored deltaLink is no longer valid.

    Callers should catch this and retry with delta_link=None (fresh sync).
    """


def mark_as_deleted(item: dict[str, Any]) -> dict[str, Any]:
    """Add _sync_metadata marking an item as delta-removed.

    Microsoft Graph delta responses include @removed for deleted items.
    This translates that into our internal sync metadata format so
    downstream services handle deletions uniformly.
    """
    item["_sync_metadata"] = {
        "sync_action": "DELETED",
        "sync_type": "INCREMENTAL",
        "inferred": True,
        "source": "delta_removed",
    }
    return item


def extract_collection_page(
    response: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    """Extract list payload and optional @odata.nextLink from Graph responses."""
    if not isinstance(response, dict):
        return [], None

    value = response.get("value")
    items = value if isinstance(value, list) else []

    next_link = response.get("@odata.nextLink")
    if not isinstance(next_link, str) or not next_link:
        next_link = None

    return items, next_link


def _is_delta_expired_exception(exc: Exception) -> bool:
    """Check if an exception signals an expired deltaLink."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 410:
        return True
    error_msg = str(exc).lower()
    return any(s in error_msg for s in _EXPIRED_DELTA_SUBSTRINGS)


def _is_delta_expired_response(resp: dict[str, Any]) -> bool:
    """Check if a JSON response body contains delta-expiry error codes."""
    if "error" not in resp:
        return False
    error_code = resp.get("error", {}).get("code", "").lower()
    return error_code in _EXPIRED_DELTA_ERROR_CODES


async def paginate_delta(
    client: httpx.AsyncClient,
    *,
    initial_endpoint: str,
    initial_params: dict[str, Any],
    stored_delta_url: str | None = None,
    max_pages: int = 500,
    enrich_item: Callable[[dict[str, Any]], None] | None = None,
    label: str = "Microsoft Graph",
) -> DeltaSyncResult:
    """Generic Microsoft Graph delta pagination with expiry detection.

    Handles the three-source request pattern (nextLink → stored deltaLink →
    fresh endpoint), @removed detection, per-item enrichment, and deltaLink
    extraction on the final page.

    Args:
        client: Authenticated httpx client with Bearer header already set.
        initial_endpoint: Graph API path for a fresh delta query
            (e.g. "/v1.0/me/calendarView/delta").
        initial_params: Query params for the fresh delta query.
        stored_delta_url: Opaque deltaLink URL from a previous sync.
            If provided and expired, raises DeltaExpiredError.
        max_pages: Safety limit to prevent infinite pagination.
        enrich_item: Optional callback to mutate each item in-place
            (e.g. add teamId/channelId context). Called for every item
            including deleted ones.
        label: Human-readable label for log messages.

    Returns:
        DeltaSyncResult with collected items and the new deltaLink (if any).

    Raises:
        DeltaExpiredError: When stored_delta_url is invalid/expired.
            Caller should retry with stored_delta_url=None.
    """
    using_stored = stored_delta_url is not None
    all_items: list[dict[str, Any]] = []
    next_link: str | None = None
    new_delta_link: str | None = None

    for page_num in range(max_pages):
        try:
            if next_link:
                resp = await graph_api_request_url(client, next_link)
            elif stored_delta_url:
                resp = await graph_api_request_url(client, stored_delta_url)
                # Only use the stored URL for the very first request
                stored_delta_url = None
            else:
                resp = await graph_api_request(client, initial_endpoint, params=initial_params)
        except httpx.HTTPStatusError as exc:
            if using_stored and page_num == 0 and exc.response.status_code == 410:
                raise DeltaExpiredError(f"{label}: stored deltaLink expired (HTTP 410)") from exc
            raise
        except Exception as exc:
            if using_stored and page_num == 0 and _is_delta_expired_exception(exc):
                raise DeltaExpiredError(f"{label}: stored deltaLink expired") from exc
            raise

        if not isinstance(resp, dict):
            break

        # Check for error response in JSON body (expired delta)
        if using_stored and page_num == 0 and _is_delta_expired_response(resp):
            error_code = resp.get("error", {}).get("code", "")
            raise DeltaExpiredError(f"{label}: deltaLink invalid (error={error_code})")

        # Extract items
        if "value" in resp and isinstance(resp["value"], list):
            for item in resp["value"]:
                if "@removed" in item:
                    logger.debug(
                        "%s delta sync: detected deleted item id=%s reason=%s",
                        label,
                        item.get("id"),
                        item.get("@removed", {}).get("reason"),
                    )
                    mark_as_deleted(item)

                if enrich_item is not None:
                    enrich_item(item)

                all_items.append(item)

        # Follow pagination or capture deltaLink on last page
        next_link = resp.get("@odata.nextLink")
        if not next_link:
            new_delta_link = resp.get("@odata.deltaLink")
            if new_delta_link:
                logger.info(
                    "%s delta sync complete after %d page(s), new deltaLink received",
                    label,
                    page_num + 1,
                )
            break

    return DeltaSyncResult(items=all_items, delta_link=new_delta_link)
