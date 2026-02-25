from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.providers._google_api import (
    calendar_api_request,
    get_access_token,
)

logger = get_logger(__name__)


@dataclass
class FetchEventsResult:
    """Result from fetch_events containing events and optional Google sync token."""

    events: list[dict[str, Any]]
    sync_token: str | None = None


def _is_google_sync_token_expired_exception(exc: Exception) -> bool:
    """Detect token-expiration style errors from raised exceptions."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 410:
        return True
    message = str(exc).lower()
    return (
        "410" in message
        or "fullsyncrequired" in message
        or "sync token is no longer valid" in message
        or ("sync token" in message and "valid" in message)
        or "gone" in message
    )


async def fetch_events_with_cursor(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    sync_token: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> FetchEventsResult:
    """
    Fetch Google Calendar events via direct Google API with syncToken incremental support.

    OAuth tokens are managed by Nango (token storage + refresh) but API calls
    bypass the Nango proxy to avoid proxy request quota limits.

    Business rules:
    - Initial/full sync: bounded window (90 days past to 365 days future).
    - Incremental sync: use Google syncToken (equivalent to delta cursor).
    - If syncToken is invalid/expired, fall back to a fresh bounded full sync.

    API constraint:
    - Requests with `syncToken` must NOT include timeMin/timeMax/updatedMin/orderBy.
    """
    _ = data_type  # intentionally unused; signature kept for registry compatibility
    _ = last_synced_at  # retained in signature for call-site compatibility
    provider = provider_config_key or to_nango_connect_integration_key(
        IntegrationType.GOOGLE_CALENDAR
    )
    now = datetime.now(UTC)

    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    # Force first sync to use bounded bootstrap window even if metadata still has a token.
    if is_initial and sync_token:
        logger.info(
            "Google Calendar initial sync ignoring stored syncToken for connection=%s",
            connection_id,
        )
        sync_token = None

    using_sync_token = bool(sync_token)
    if using_sync_token:
        # Incremental token query: only include parameters allowed with syncToken.
        params: dict[str, Any] = {
            "syncToken": sync_token,
            "singleEvents": True,
            "showDeleted": True,
            "maxResults": 2500,
        }
    else:
        # Full bootstrap query: bounded window to control ingestion cost.
        time_min = now - timedelta(days=90)
        time_max = now + timedelta(days=365)
        time_min = _ensure_utc(time_min)
        time_max = _ensure_utc(time_max)

        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            # Keep cancelled events in snapshots so downstream status mapping is consistent.
            "showDeleted": True,
            "maxResults": 2500,
            "orderBy": "startTime",
        }

    endpoint = "/calendar/v3/calendars/primary/events"

    token = await get_access_token(nango, connection_id, provider)

    all_items: list[dict[str, Any]] = []
    page_token: str | None = None
    next_sync_token: str | None = None
    max_pages = 50  # Safety limit to prevent infinite loops

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as client:
        for page_index in range(max_pages):
            if page_token:
                params["pageToken"] = page_token
            elif "pageToken" in params:
                del params["pageToken"]

            try:
                resp = await calendar_api_request(client, endpoint, params=params)
            except httpx.HTTPStatusError as exc:
                # A stale sync token should not break calendar sync forever.
                if (
                    using_sync_token
                    and page_index == 0
                    and _is_google_sync_token_expired_exception(exc)
                ):
                    logger.warning(
                        "Google Calendar syncToken expired for connection=%s;"
                        " falling back to full sync",
                        connection_id,
                    )
                    return await fetch_events_with_cursor(
                        nango=nango,
                        connection_id=connection_id,
                        is_initial=True,
                        last_synced_at=last_synced_at,
                        sync_token=None,
                        data_type=data_type,
                        provider_config_key=provider_config_key,
                    )
                raise
            except Exception as exc:
                if (
                    using_sync_token
                    and page_index == 0
                    and _is_google_sync_token_expired_exception(exc)
                ):
                    logger.warning(
                        "Google Calendar syncToken expired for connection=%s;"
                        " falling back to full sync",
                        connection_id,
                    )
                    return await fetch_events_with_cursor(
                        nango=nango,
                        connection_id=connection_id,
                        is_initial=True,
                        last_synced_at=last_synced_at,
                        sync_token=None,
                        data_type=data_type,
                        provider_config_key=provider_config_key,
                    )
                raise

            if "items" in resp and isinstance(resp["items"], list):
                all_items.extend(resp["items"])

            # Check for next page
            page_token = resp.get("nextPageToken")
            if not page_token:
                sync_token_candidate = resp.get("nextSyncToken")
                if isinstance(sync_token_candidate, str) and sync_token_candidate:
                    next_sync_token = sync_token_candidate
                break

    logger.info(
        "Google Calendar fetch complete: events=%d initial=%s used_sync_token=%s",
        len(all_items),
        is_initial,
        using_sync_token,
    )
    return FetchEventsResult(events=all_items, sync_token=next_sync_token)


async def fetch_events(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    sync_token: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """Backward-compatible list-only fetcher used by registry-based code paths."""
    result = await fetch_events_with_cursor(
        nango=nango,
        connection_id=connection_id,
        is_initial=is_initial,
        last_synced_at=last_synced_at,
        sync_token=sync_token,
        data_type=data_type,
        provider_config_key=provider_config_key,
    )
    return result.events
