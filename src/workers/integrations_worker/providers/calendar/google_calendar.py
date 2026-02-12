from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)


@dataclass
class FetchEventsResult:
    """Result from fetch_events containing events and optional Google sync token."""

    events: list[dict[str, Any]]
    sync_token: str | None = None


def _is_google_sync_token_expired_error(payload: dict[str, Any]) -> bool:
    """Detect token-expiration style errors from Google Calendar responses."""
    error = payload.get("error")
    if not isinstance(error, dict):
        return False

    code = str(error.get("code", "")).lower()
    message = str(error.get("message", "")).lower()
    return (
        code == "410"
        or "fullsyncrequired" in message
        or "sync token is no longer valid" in message
        or "sync token" in message
        and "valid" in message
        or "gone" in message
    )


def _is_google_sync_token_expired_exception(exc: Exception) -> bool:
    """Detect token-expiration style errors from raised exceptions."""
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
    Fetch Google Calendar events via Nango proxy with syncToken incremental support.

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

    # Google endpoint; Nango proxy takes endpoint path only
    endpoint = "/calendar/v3/calendars/primary/events"

    all_items: list[dict[str, Any]] = []
    page_token: str | None = None
    next_sync_token: str | None = None
    max_pages = 50  # Safety limit to prevent infinite loops

    for page_index in range(max_pages):
        # Add page token if we have one from previous request
        if page_token:
            params["pageToken"] = page_token
        elif "pageToken" in params:
            del params["pageToken"]

        # NangoClient.proxy_request returns dict; normalize 'items'
        try:
            resp = await nango.proxy_request(
                provider_config_key=provider,
                connection_id=connection_id,
                endpoint=endpoint,
                params=params,
            )
        except Exception as exc:
            # A stale sync token should not break calendar sync forever.
            # We recover by doing a bounded full sync and obtaining a fresh token.
            if (
                using_sync_token
                and page_index == 0
                and _is_google_sync_token_expired_exception(exc)
            ):
                logger.warning(
                    "Google Calendar syncToken expired for connection=%s; falling back to full sync",
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

        if isinstance(resp, dict):
            # Some proxies return token errors in payload instead of raising exceptions.
            if using_sync_token and page_index == 0 and _is_google_sync_token_expired_error(resp):
                logger.warning(
                    "Google Calendar syncToken rejected for connection=%s; falling back to full sync",
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

            if "items" in resp and isinstance(resp["items"], list):
                all_items.extend(resp["items"])
            elif "results" in resp and isinstance(resp["results"], list):
                all_items.extend(resp["results"])

            # Check for next page
            page_token = resp.get("nextPageToken")
            if not page_token:
                sync_token_candidate = resp.get("nextSyncToken")
                if isinstance(sync_token_candidate, str) and sync_token_candidate:
                    next_sync_token = sync_token_candidate
                break
        else:
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
