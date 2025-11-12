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
    Fetch Outlook Calendar events via Nango proxy.

    Uses Microsoft Graph API: /v1.0/me/calendar/events

    - Initial: last 90 days to next 365 days
    - Incremental: from last_synced_at - 1 day to +365 days

    Microsoft Graph uses $filter with startDateTime and endDateTime for date filtering.
    """
    _ = data_type  # intentionally unused; signature kept for registry compatibility
    provider = provider_config_key or to_nango_connect_integration_key(
        IntegrationType.OUTLOOK_CALENDAR
    )
    now = datetime.now(UTC)

    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    if is_initial or not last_synced_at:
        time_min = now - timedelta(days=90)
    else:
        # backoff 1 day to catch late updates
        time_min = last_synced_at - timedelta(days=1)
    time_max = now + timedelta(days=365)

    time_min = _ensure_utc(time_min)
    time_max = _ensure_utc(time_max)

    # Microsoft Graph API parameters
    # Using $filter for date range and $orderby for sorting
    # $top limits results (max 999 for calendar events)
    params = {
        "$filter": f"start/dateTime ge '{time_min.isoformat()}' and start/dateTime le '{time_max.isoformat()}'",
        "$orderby": "start/dateTime",
        "$top": "999",
        "$select": "id,subject,body,start,end,location,locations,attendees,organizer,webLink,isAllDay,isCancelled,isOrganizer,responseStatus,reminderMinutesBeforeStart,isReminderOn,recurrence,seriesMasterId,type,onlineMeeting,onlineMeetingUrl,createdDateTime,lastModifiedDateTime",
    }

    # Microsoft Graph endpoint for primary calendar events
    endpoint = "/v1.0/me/calendar/events"

    # NangoClient.proxy_request returns dict; normalize 'value' (Graph uses 'value' not 'items')
    resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
        params=params,
    )

    items = []
    if isinstance(resp, dict):
        if "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]
        elif "items" in resp and isinstance(resp["items"], list):
            # Fallback if Nango normalizes to 'items'
            items = resp["items"]

    return items
