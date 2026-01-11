from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)


@dataclass
class FetchEventsResult:
    """Result from fetch_events containing events and optional delta token."""

    events: list[dict[str, Any]]
    delta_link: str | None = None


def _extract_pagination_params(next_link: str) -> tuple[str, dict[str, Any]]:
    """
    Extract endpoint path and query params from Microsoft Graph @odata.nextLink or @odata.deltaLink.

    Microsoft Graph returns full URLs like:
    https://graph.microsoft.com/v1.0/me/calendarView/delta?$skiptoken=xxx...
    https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=xxx...

    We need to extract the path and query params for Nango's proxy_request.
    """
    parsed = urlparse(next_link)
    endpoint = parsed.path  # e.g., /v1.0/me/calendar/events or /v1.0/me/calendar/events/delta

    # Parse query string into dict (parse_qs returns lists, we want single values)
    query_dict = parse_qs(parsed.query)
    params: dict[str, Any] = {k: v[0] if len(v) == 1 else v for k, v in query_dict.items()}

    return endpoint, params


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
    Fetch Outlook Calendar events via Nango proxy.

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

    if delta_link:
        # True incremental sync: use stored deltaLink
        # This returns ONLY events that changed since the deltaLink was issued
        initial_endpoint, initial_params = _extract_pagination_params(delta_link)
        using_stored_delta_link = True
        logger.info(
            "Outlook Calendar: using stored deltaLink (true incremental - only changed events)",
        )
    else:
        # Fresh delta query (initial sync OR first incremental without deltaLink)
        # Use calendarView/delta which returns FULL event properties
        # (events/delta returns LIMITED properties - missing organizer, onlineMeeting, etc.)
        # Date range: 90 days past to 365 days future
        start_dt = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        end_dt = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        initial_params: dict[str, Any] = {
            "startDateTime": start_dt,
            "endDateTime": end_dt,
        }
        initial_endpoint = "/v1.0/me/calendarView/delta"
        logger.info(
            "Outlook Calendar: fresh calendarView/delta query (start=%s, end=%s)",
            start_dt,
            end_dt,
        )

    all_items: list[dict[str, Any]] = []
    next_link: str | None = None
    new_delta_link: str | None = None
    max_pages = 50  # Safety limit to prevent infinite loops

    for page_num in range(max_pages):
        # Determine endpoint and params for this request
        if next_link:
            endpoint, params = _extract_pagination_params(next_link)
        else:
            endpoint, params = initial_endpoint, initial_params

        # NangoClient.proxy_request returns dict; normalize 'value' (Graph uses 'value' not 'items')
        try:
            resp = await nango.proxy_request(
                provider_config_key=provider,
                connection_id=connection_id,
                endpoint=endpoint,
                params=params,
            )
        except Exception as e:
            # Handle expired deltaLink (Microsoft returns 410 Gone or similar)
            # If using stored deltaLink and get an error, retry with fresh delta query
            if using_stored_delta_link and page_num == 0:
                error_msg = str(e).lower()
                if "410" in error_msg or "gone" in error_msg or "sync state" in error_msg:
                    logger.warning(
                        "Outlook Calendar: stored deltaLink expired, retrying with fresh delta query"
                    )
                    # Retry without deltaLink - recursive call with delta_link=None
                    return await fetch_events(
                        nango=nango,
                        connection_id=connection_id,
                        is_initial=False,
                        last_synced_at=last_synced_at,
                        delta_link=None,  # Force fresh delta query
                        data_type=data_type,
                        provider_config_key=provider_config_key,
                    )
            # Re-raise if not a deltaLink expiration issue
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
            elif "items" in resp and isinstance(resp["items"], list):
                # Fallback if Nango normalizes to 'items'
                all_items.extend(resp["items"])

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
