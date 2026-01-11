from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.service.integrations.nango_client import NangoClient


async def fetch_events(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Google Calendar events via Nango proxy.

    - Initial: last 90 days to next 365 days
    - Incremental: events modified since last_synced_at (using updatedMin)
      PLUS new events starting after last_synced_at

    Implements pagination using nextPageToken to fetch all events.
    """
    _ = data_type  # intentionally unused; signature kept for registry compatibility
    provider = provider_config_key or to_nango_connect_integration_key(
        IntegrationType.GOOGLE_CALENDAR
    )
    now = datetime.now(UTC)

    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    if is_initial or not last_synced_at:
        # Initial sync: fetch events from 90 days ago to 365 days in future
        time_min = now - timedelta(days=90)
        time_max = now + timedelta(days=365)
        time_min = _ensure_utc(time_min)
        time_max = _ensure_utc(time_max)

        params: dict[str, Any] = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "maxResults": 2500,
            "orderBy": "startTime",
        }
    else:
        # Incremental sync: use updatedMin to get ALL events modified since last sync
        # This catches updates to past events that wouldn't be returned by timeMin filter
        updated_min = _ensure_utc(last_synced_at - timedelta(hours=1))  # 1 hour buffer

        params = {
            "updatedMin": updated_min.isoformat(),
            "singleEvents": True,
            "maxResults": 2500,
            "orderBy": "updated",  # Order by update time for incremental
            "showDeleted": True,  # Include cancelled events for proper status tracking
        }

    # Google endpoint; Nango proxy takes endpoint path only
    endpoint = "/calendar/v3/calendars/primary/events"

    all_items: list[dict[str, Any]] = []
    page_token: str | None = None
    max_pages = 50  # Safety limit to prevent infinite loops

    for _ in range(max_pages):
        # Add page token if we have one from previous request
        if page_token:
            params["pageToken"] = page_token
        elif "pageToken" in params:
            del params["pageToken"]

        # NangoClient.proxy_request returns dict; normalize 'items'
        resp = await nango.proxy_request(
            provider_config_key=provider,
            connection_id=connection_id,
            endpoint=endpoint,
            params=params,
        )

        if isinstance(resp, dict):
            if "items" in resp and isinstance(resp["items"], list):
                all_items.extend(resp["items"])
            elif "results" in resp and isinstance(resp["results"], list):
                all_items.extend(resp["results"])

            # Check for next page
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        else:
            break

    return all_items
