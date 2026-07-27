"""Google Calendar provider — fetches events via direct Google API with syncToken support.

OAuth tokens are read from local credentials (custom OAuth).
API calls go directly to Google (never through a proxy).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.calendar_cursor_metadata import (
    CalendarCursorMissingError,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers._google_api import (
    calendar_api_request,
)

logger = get_logger(__name__)


@dataclass
class FetchEventsResult:
    """Events fetched from Google Calendar with an optional syncToken for next delta."""

    events: list[dict[str, Any]]
    sync_token: str | None = None


def _is_google_sync_token_expired_exception(exc: Exception) -> bool:
    """Detect Google's "sync token expired" errors across response formats.

    Google signals token expiry inconsistently — sometimes HTTP 410, sometimes
    error messages with varying wording. This catches all known variants so
    the caller can fall back to a full sync instead of crashing.
    """
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
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    sync_token: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> FetchEventsResult:
    """Fetch Google Calendar events with syncToken-based incremental sync.

    Called by calendar org_task (Tier 2) and integration_task (Tier 3) during
    the "no session held" phase. Returns raw Google API event dicts for the
    adapter layer to normalize.

    Args:
        token_info: OAuth token info for this integration.
        connection_id: Integration connection ID (for logging).
        is_initial: True on first-ever sync — uses bounded time window.
        last_synced_at: Unused; kept for provider registry interface compatibility.
        sync_token: Google syncToken from previous sync for delta fetching.
        data_type: Unused; kept for provider registry signature compatibility.
        provider_config_key: Unused; kept for interface compatibility.

    Returns:
        FetchEventsResult with events and a new syncToken for next sync.

    Raises:
        httpx.HTTPStatusError: On non-recoverable Google API errors.
    """
    # Do not remove unused params — registry.get_fetcher() calls all providers
    # with the same kwargs; removing these breaks the shared interface.
    _ = data_type
    _ = last_synced_at
    _ = provider_config_key
    now = datetime.now(UTC)

    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    # Force first sync to use bounded bootstrap window even if stale metadata
    # still carries a token from a previously deleted integration.
    if is_initial and sync_token:
        logger.info(
            "Google Calendar initial sync ignoring stored syncToken for connection=%s",
            connection_id,
        )
        sync_token = None

    using_sync_token = bool(sync_token)
    if using_sync_token:
        # Google API constraint: syncToken requests must NOT include
        # timeMin/timeMax/updatedMin/orderBy — the API returns 400 otherwise.
        params: dict[str, Any] = {
            "syncToken": sync_token,
            "singleEvents": True,
            "showDeleted": True,
            "maxResults": 2500,
        }
    else:
        # Bounded window controls ingestion cost on initial sync — 90 days
        # past captures recent meeting history, 365 days future covers
        # long-range scheduled events (QBRs, annual reviews).
        time_min = _ensure_utc(now - timedelta(days=90))
        time_max = _ensure_utc(now + timedelta(days=365))

        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            # Google omits nextSyncToken when orderBy is present, which would
            # make every later webhook repeat this full bounded fetch.
            # showDeleted=True so downstream status mapping sees cancellations
            # instead of silently losing events that were cancelled externally.
            "showDeleted": True,
            "maxResults": 2500,
        }

    endpoint = "/calendar/v3/calendars/primary/events"

    async def _do_fetch(token: str) -> FetchEventsResult:
        # Copy params so a 401 retry starts with clean state — the outer
        # `params` dict would still carry a stale pageToken from the failed attempt.
        fetch_params = dict(params)
        all_items: list[dict[str, Any]] = []
        page_token: str | None = None
        next_sync_token: str | None = None
        # Safety cap — Google pagination should terminate naturally via missing
        # nextPageToken, but a malformed response could loop indefinitely.
        max_pages = 50

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            for page_index in range(max_pages):
                if page_token:
                    fetch_params["pageToken"] = page_token
                elif "pageToken" in fetch_params:
                    del fetch_params["pageToken"]

                try:
                    resp = await calendar_api_request(client, endpoint, params=fetch_params)
                except httpx.HTTPStatusError as exc:
                    # Stale syncToken → fall back to full sync instead of failing
                    # permanently. Only on the first page — mid-pagination 410s
                    # indicate a real API problem, not token expiry.
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
                        # Recurse through the outer function so the retry
                        # wrapper covers the fallback too
                        return await fetch_events_with_cursor(
                            token_info=token_info,
                            connection_id=connection_id,
                            is_initial=True,
                            last_synced_at=last_synced_at,
                            sync_token=None,
                            data_type=data_type,
                            provider_config_key=provider_config_key,
                        )
                    raise
                except Exception as exc:
                    # Same fallback for non-HTTP exceptions (e.g. JSON decode
                    # errors wrapping a 410 message from Google).
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
                            token_info=token_info,
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

                page_token = resp.get("nextPageToken")
                if not page_token:
                    # Google returns nextSyncToken only on the final page —
                    # save it for the next incremental sync.
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

    result: FetchEventsResult = await call_with_token_retry(token_info, _do_fetch)
    if result.sync_token is None:
        # A terminal page without a cursor cannot support the next webhook.
        # Fail before persistence so it is visible and cannot become a full-sync loop.
        raise CalendarCursorMissingError(
            f"Google Calendar completed without nextSyncToken for connection={connection_id}"
        )
    return result


async def fetch_events(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    sync_token: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """List-only wrapper for provider registry callers that don't need the syncToken.

    Do not remove — the provider registry dispatches to this function by name.
    Use fetch_events_with_cursor() directly when you need the returned syncToken.
    """
    result = await fetch_events_with_cursor(
        token_info=token_info,
        connection_id=connection_id,
        is_initial=is_initial,
        last_synced_at=last_synced_at,
        sync_token=sync_token,
        data_type=data_type,
        provider_config_key=provider_config_key,
    )
    return result.events
