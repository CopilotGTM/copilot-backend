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
    - Incremental: from last_synced_at - 1 day to +365 days
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
        time_min = now - timedelta(days=90)
    else:
        # backoff 1 day to catch late updates
        time_min = last_synced_at - timedelta(days=1)
    time_max = now + timedelta(days=365)

    time_min = _ensure_utc(time_min)
    time_max = _ensure_utc(time_max)

    params = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": True,
        "maxResults": 2500,
        "orderBy": "startTime",
    }

    # Google endpoint; Nango proxy takes endpoint path only
    endpoint = "/calendar/v3/calendars/primary/events"

    # NangoClient.proxy_request returns dict; normalize 'items'
    resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
        params=params,
    )

    items = []
    if isinstance(resp, dict):
        if "items" in resp and isinstance(resp["items"], list):
            items = resp["items"]
        elif "results" in resp and isinstance(resp["results"], list):
            items = resp["results"]

    return items
