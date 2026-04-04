"""Outlook Calendar fetcher via Microsoft Graph API (direct calls).

Fetches calendar events from Outlook/Office 365 calendars.
OAuth tokens are managed by custom OAuth via token_resolution module.
API calls go directly to Microsoft Graph.

Uses Microsoft Graph calendarView/delta for efficient incremental sync:
- Initial sync: calendarView/delta with startDateTime/endDateTime (90 days past to 365 days future)
- Incremental sync: Use stored deltaLink (returns ONLY changed events)

Microsoft Graph API documentation:
- calendarView delta: https://learn.microsoft.com/en-us/graph/api/calendarview-delta
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers._delta_sync import (
    DeltaExpiredError,
    paginate_delta,
)
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_request,
)

logger = get_logger(__name__)


@dataclass
class FetchEventsResult:
    """Result from fetch_events containing events and optional delta token."""

    events: list[dict[str, Any]]
    delta_link: str | None = None


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


def _needs_identity_hydration(event: dict[str, Any]) -> bool:
    if "@removed" in event or event.get("_sync_metadata", {}).get("sync_action") == "DELETED":
        return False
    if isinstance(event.get("iCalUId"), str) and event["iCalUId"]:
        return False
    return isinstance(event.get("id"), str) and bool(event["id"])


async def _fetch_event_details(
    client: httpx.AsyncClient,
    event_id: str,
) -> dict[str, Any] | None:
    endpoints = (
        f"/v1.0/me/events/{event_id}",
        f"/v1.0/me/calendar/events/{event_id}",
    )
    for endpoint in endpoints:
        for attempt in range(3):
            try:
                return await graph_api_request(client, endpoint)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    break
                if exc.response.status_code == 429:
                    retry_after = exc.response.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else float(attempt + 1)
                    )
                    logger.warning(
                        "Outlook Calendar hydration rate limited for %s; retrying in %.1fs (attempt %d/3)",
                        endpoint,
                        delay,
                        attempt + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception:
                raise
        else:
            continue
    return None


def _merge_event_details(
    base_event: dict[str, Any], detailed_event: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(detailed_event)
    merged.update(
        {
            key: value
            for key, value in base_event.items()
            if key
            in {
                "id",
                "seriesMasterId",
                "type",
                "start",
                "end",
                "originalStart",
                "@removed",
                "_sync_metadata",
            }
        }
    )
    if "_sync_metadata" in base_event:
        merged["_sync_metadata"] = base_event["_sync_metadata"]
    return merged


async def _hydrate_missing_identity_events(
    client: httpx.AsyncClient,
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    candidates = [event for event in events if _needs_identity_hydration(event)]
    if not candidates:
        return events, 0, 0

    replacement_by_id: dict[str, dict[str, Any]] = {}

    # Re-fetch the occurrence itself by ID. We do not derive the canonical UID
    # from the series master because that can collapse multiple instances.
    for event in candidates:
        event_id = str(event["id"])
        details = await _fetch_event_details(client, event_id)
        if details and isinstance(details.get("iCalUId"), str) and details["iCalUId"]:
            replacement_by_id[event_id] = _merge_event_details(event, details)

    hydrated_count = 0
    unresolved_count = 0
    hydrated_events: list[dict[str, Any]] = []
    for event in events:
        event_id = event.get("id")
        replacement = replacement_by_id.get(event_id) if isinstance(event_id, str) else None
        if replacement is not None:
            hydrated_events.append(replacement)
            hydrated_count += 1
            continue
        if _needs_identity_hydration(event):
            unresolved_count += 1
        hydrated_events.append(event)

    return hydrated_events, hydrated_count, unresolved_count


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
    token_info: IntegrationTokenInfo,
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
    - Deleted events with @removed annotation (we mark these as mailbox-level deletions)
    - A new deltaLink to use for the next sync

    Args:
        token_info: OAuth token info for this integration.
        delta_link: Optional stored deltaLink from previous sync. If provided,
                    only events changed since that deltaLink are returned.

    Returns:
        FetchEventsResult with events and new deltaLink
    """
    _ = data_type  # intentionally unused; signature kept for registry compatibility
    _ = provider_config_key  # no longer needed — tokens come from local credentials
    now = datetime.now(UTC)

    stored_delta_url: str | None = None
    if delta_link:
        stored_delta_url = delta_link
        logger.info(
            "Outlook Calendar: using stored deltaLink (true incremental - only changed events)",
        )

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

    async def _do_fetch(token: str) -> tuple[list[dict[str, Any]], str | None]:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "odata.maxpagesize=50",
            },
            timeout=30.0,
        ) as client:
            delta_result = await paginate_delta(
                client,
                initial_endpoint=initial_endpoint,
                initial_params=initial_params,
                stored_delta_url=stored_delta_url,
                label="Outlook Calendar",
            )

            items = delta_result.items

            # Hydrate events missing iCalUId (needed for cross-provider matching)
            hydrated_count = 0
            unresolved_hydration_count = 0
            if items:
                (
                    items,
                    hydrated_count,
                    unresolved_hydration_count,
                ) = await _hydrate_missing_identity_events(client, items)
                if hydrated_count > 0 or unresolved_hydration_count > 0:
                    logger.info(
                        "Outlook Calendar: hydrated %d event(s) missing iCalUId;"
                        " %d still unresolved",
                        hydrated_count,
                        unresolved_hydration_count,
                    )

        return items, delta_result.delta_link

    try:
        all_items, new_delta_link = await call_with_token_retry(token_info, _do_fetch)
    except DeltaExpiredError:
        logger.warning(
            "Outlook Calendar: stored deltaLink expired, retrying with fresh delta query"
        )
        return await fetch_events(
            token_info=token_info,
            connection_id=connection_id,
            is_initial=False,
            last_synced_at=last_synced_at,
            delta_link=None,
        )

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
        bool(delta_link),
    )
    return FetchEventsResult(events=all_items, delta_link=new_delta_link)
