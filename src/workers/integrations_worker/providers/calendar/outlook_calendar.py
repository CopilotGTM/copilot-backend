"""Outlook Calendar fetcher via Microsoft Graph API (direct calls).

Fetches calendar events from Outlook/Office 365 calendars.
OAuth tokens are managed by Nango (token storage + refresh) but API calls
bypass the Nango proxy to avoid proxy request quota limits.

Uses Microsoft Graph calendarView/delta for efficient incremental sync:
- Initial sync: calendarView/delta with startDateTime/endDateTime (90 days past to 365 days future)
- Incremental sync: Use stored deltaLink (returns ONLY changed events)

Microsoft Graph API documentation:
- calendarView delta: https://learn.microsoft.com/en-us/graph/api/calendarview-delta
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.providers._microsoft_graph import (
    get_access_token,
    graph_api_request,
    graph_api_request_url,
)

logger = get_logger(__name__)


@dataclass
class FetchEventsResult:
    """Result from fetch_events containing events and optional delta token."""

    events: list[dict[str, Any]]
    delta_link: str | None = None


def _mark_as_deleted(event: dict[str, Any]) -> dict[str, Any]:
    """
    Mark an event as deleted/cancelled for proper handling downstream.

    Microsoft Graph delta sync returns deleted items with @removed annotation.
    We translate this to isCancelled=true and add _sync_metadata for proper action tracking.
    """
    event["isCancelled"] = True
    event["_sync_metadata"] = {
        "sync_action": "DELETED",
        "sync_type": "INCREMENTAL",
        "inferred": True,
        "source": "delta_removed",
    }
    return event


def _parse_event_datetime(event: dict[str, Any]) -> datetime | None:
    """
    Parse the start datetime from an event.

    Microsoft Graph events have start.dateTime in ISO format.
    Returns None if parsing fails or event has no start time.
    """
    start = event.get("start")
    if not start:
        return None

    dt_str = start.get("dateTime")
    if not dt_str:
        return None

    try:
        # Microsoft Graph returns ISO format, may or may not have timezone
        # Try parsing with timezone first, then without
        if "Z" in dt_str or "+" in dt_str or "-" in dt_str[10:]:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        else:
            # No timezone, assume UTC
            return datetime.fromisoformat(dt_str).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _filter_old_events(
    events: list[dict[str, Any]],
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """
    Filter out events that ended before the cutoff date.

    Args:
        events: List of events to filter
        cutoff: Datetime cutoff - events ending before this are excluded

    Returns:
        Tuple of (filtered events, count of excluded events)

    Note: Deleted events (with @removed) are always kept regardless of date,
    as we need to process deletions even for old events.
    """
    filtered: list[dict[str, Any]] = []
    excluded_count = 0

    for event in events:
        # Always keep deleted events - we need to process deletions
        if "@removed" in event or event.get("_sync_metadata", {}).get("sync_action") == "DELETED":
            filtered.append(event)
            continue

        # Parse event start time
        event_dt = _parse_event_datetime(event)
        if event_dt is None:
            # Can't parse date, keep the event to be safe
            filtered.append(event)
            continue

        # Exclude events that started before cutoff
        if event_dt < cutoff:
            excluded_count += 1
            continue

        filtered.append(event)

    return filtered, excluded_count


async def fetch_events(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    delta_link: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> FetchEventsResult:
    """
    Fetch Outlook Calendar events via direct Microsoft Graph API calls.

    Uses Microsoft Graph API calendarView/delta endpoint which returns FULL event properties
    (unlike events/delta which returns limited properties).

    - Fresh sync (no deltaLink): /v1.0/me/calendarView/delta with startDateTime/endDateTime
      (90 days past to 365 days future)
    - Incremental sync with deltaLink: Uses stored deltaLink for true incremental
      (only changed events, date range encoded in token)

    Microsoft Graph delta sync returns:
    - Added/modified events as regular event objects with FULL properties
    - Deleted events with @removed annotation (we mark these with isCancelled=true)
    - A new deltaLink to use for the next sync

    Args:
        delta_link: Optional stored deltaLink from previous sync. If provided,
                    only events changed since that deltaLink are returned.

    Returns:
        FetchEventsResult with events and new deltaLink
    """
    _ = data_type  # intentionally unused; signature kept for registry compatibility
    provider = provider_config_key or to_nango_connect_integration_key(
        IntegrationType.OUTLOOK_CALENDAR
    )
    now = datetime.now(UTC)

    # Track if we're using a stored deltaLink for true incremental sync
    using_stored_delta_link = False
    stored_delta_url: str | None = None

    if delta_link:
        # True incremental sync: use stored deltaLink URL directly
        stored_delta_url = delta_link
        using_stored_delta_link = True
        logger.info(
            "Outlook Calendar: using stored deltaLink (true incremental - only changed events)",
        )

    # Fresh delta query params (used when no delta_link and no next_link)
    start_dt = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    end_dt = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    initial_endpoint = "/v1.0/me/calendarView/delta"
    initial_params: dict[str, Any] = {
        "startDateTime": start_dt,
        "endDateTime": end_dt,
    }
    if not delta_link:
        logger.info(
            "Outlook Calendar: fresh calendarView/delta query (start=%s, end=%s)",
            start_dt,
            end_dt,
        )

    token = await get_access_token(nango, connection_id, provider)

    all_items: list[dict[str, Any]] = []
    next_link: str | None = None
    new_delta_link: str | None = None
    max_pages = 500  # Safety limit

    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token}",
            "Prefer": "odata.maxpagesize=50",
        },
        timeout=30.0,
    ) as client:
        for page_num in range(max_pages):
            try:
                if next_link:
                    resp = await graph_api_request_url(client, next_link)
                elif stored_delta_url:
                    resp = await graph_api_request_url(client, stored_delta_url)
                    stored_delta_url = None  # only used for the first request
                else:
                    resp = await graph_api_request(client, initial_endpoint, params=initial_params)
            except httpx.HTTPStatusError as exc:
                if using_stored_delta_link and page_num == 0 and exc.response.status_code == 410:
                    logger.warning(
                        "Outlook Calendar: stored deltaLink expired (410), retrying with fresh delta query"
                    )
                    return await fetch_events(
                        nango=nango,
                        connection_id=connection_id,
                        is_initial=False,
                        last_synced_at=last_synced_at,
                        delta_link=None,
                        data_type=data_type,
                        provider_config_key=provider_config_key,
                    )
                raise
            except Exception as e:
                # Handle expired deltaLink (Microsoft returns 410 Gone or similar)
                # If using stored deltaLink and get an error, retry with fresh delta query
                if using_stored_delta_link and page_num == 0:
                    error_msg = str(e).lower()
                    if "410" in error_msg or "gone" in error_msg or "sync state" in error_msg:
                        logger.warning(
                            "Outlook Calendar: stored deltaLink expired, retrying with fresh delta query"
                        )
                        return await fetch_events(
                            nango=nango,
                            connection_id=connection_id,
                            is_initial=False,
                            last_synced_at=last_synced_at,
                            delta_link=None,
                            data_type=data_type,
                            provider_config_key=provider_config_key,
                        )
                raise

            if isinstance(resp, dict):
                # Check for error response (deltaLink might be expired)
                if "error" in resp and using_stored_delta_link and page_num == 0:
                    error_code = resp.get("error", {}).get("code", "").lower()
                    if error_code in ("syncstatenotfound", "resyncresquired", "invalidrequest"):
                        logger.warning(
                            "Outlook Calendar: deltaLink invalid (error=%s), retrying with fresh delta query",
                            error_code,
                        )
                        return await fetch_events(
                            nango=nango,
                            connection_id=connection_id,
                            is_initial=False,
                            last_synced_at=last_synced_at,
                            delta_link=None,
                            data_type=data_type,
                            provider_config_key=provider_config_key,
                        )

                if "value" in resp and isinstance(resp["value"], list):
                    for item in resp["value"]:
                        # Check for deleted items (delta sync returns @removed annotation)
                        if "@removed" in item:
                            # Mark as deleted for downstream handling
                            logger.debug(
                                "Delta sync: detected deleted event id=%s reason=%s",
                                item.get("id"),
                                item.get("@removed", {}).get("reason"),
                            )
                            all_items.append(_mark_as_deleted(item))
                        else:
                            all_items.append(item)

                # Check for next page - Microsoft Graph uses @odata.nextLink
                # For delta sync, @odata.deltaLink indicates sync is complete (no more pages)
                next_link = resp.get("@odata.nextLink")
                if not next_link:
                    # Capture deltaLink for next sync (only present on last page of delta responses)
                    new_delta_link = resp.get("@odata.deltaLink")
                    if new_delta_link:
                        logger.info(
                            "Delta sync complete after %d pages, new deltaLink received",
                            page_num + 1,
                        )
                    break
            else:
                break

    # For incremental syncs using unbounded delta, filter out very old events
    # to avoid processing events from years ago that were modified
    if not is_initial and all_items:
        cutoff = now - timedelta(days=90)
        all_items, excluded = _filter_old_events(all_items, cutoff)
        if excluded > 0:
            logger.info(
                "Outlook Calendar: filtered %d events older than 90 days",
                excluded,
            )

    logger.info(
        "Outlook Calendar fetch complete: %d events (initial=%s, used_delta_link=%s)",
        len(all_items),
        is_initial,
        using_stored_delta_link,
    )
    return FetchEventsResult(events=all_items, delta_link=new_delta_link)
