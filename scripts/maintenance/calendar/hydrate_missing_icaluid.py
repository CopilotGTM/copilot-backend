"""Hydrate calendar events that are missing shared UID in their data_payload.

Supports both Google Calendar and Outlook Calendar events:
- Google: fetches via /calendar/v3/calendars/primary/events/{id} to get iCalUID
- Outlook: fetches via /v1.0/me/events/{id} to get iCalUId

This script:
1. Finds events where data_payload has no iCalUID (Google) or iCalUId (Outlook)
2. Looks up the integration via calendar_event_user_states
3. Fetches the full event from the provider API using the mailbox-specific ID
4. Updates data_payload with the full response (including shared UID)
5. Updates external_id, provider, occurrence_anchor, original_start_at

Dry run is the default. Use --execute to persist changes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlmodel import col

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration  # noqa: E402
from src.domain.models.organisation_tables.sync.calendar import (  # noqa: E402
    CalendarEvent,
    CalendarEventUserState,
)
from src.infra.database.sessions import acquire_org_async_session  # noqa: E402
from src.infra.utils.logger import get_logger  # noqa: E402
from src.service.integrations.nango_client import NangoClient  # noqa: E402
from src.workers.integrations_worker.providers._google_api import (  # noqa: E402
    calendar_api_request,
    get_access_token as google_get_access_token,
)
from src.workers.integrations_worker.providers._microsoft_graph import (  # noqa: E402
    get_access_token as outlook_get_access_token,
    graph_api_request,
)

logger = get_logger(__name__)

ISO_UTC_SUFFIX = "+00:00"
GOOGLE_TYPES = {IntegrationType.GOOGLE_CALENDAR}
OUTLOOK_TYPES = {IntegrationType.OUTLOOK_CALENDAR}


@dataclass
class HydrateStats:
    total_missing: int = 0
    hydrated: int = 0
    still_missing: int = 0
    api_errors: int = 0
    no_integration: int = 0


def _has_shared_uid(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    ical_uid = payload.get("iCalUID")
    if isinstance(ical_uid, str) and ical_uid:
        return True
    ical_uid = payload.get("iCalUId")
    if isinstance(ical_uid, str) and ical_uid:
        return True
    return False


def _get_provider_event_id(event: CalendarEvent) -> str | None:
    """Get the provider-specific event ID from data_payload or external_id."""
    payload = event.data_payload
    if isinstance(payload, dict):
        eid = payload.get("id")
        if isinstance(eid, str) and eid:
            return eid
    if event.external_id:
        return event.external_id
    return None


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", ISO_UTC_SUFFIX))


def _parse_iso_date(value: str) -> datetime:
    return datetime.fromisoformat(f"{value}T00:00:00{ISO_UTC_SUFFIX}")


def _format_occurrence_anchor(value: datetime | None) -> str:
    if value is None:
        return ""
    normalized = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _extract_google_identity(payload: dict) -> tuple[str | None, str, datetime | None]:
    """Extract (iCalUID, occurrence_anchor, original_start_at) from Google payload."""
    ical_uid = payload.get("iCalUID")
    if not isinstance(ical_uid, str) or not ical_uid:
        return None, "", None
    original_start = payload.get("originalStartTime")
    if isinstance(original_start, dict):
        dt_str = original_start.get("dateTime")
        if isinstance(dt_str, str) and dt_str:
            parsed = _parse_iso_datetime(dt_str)
            return ical_uid, _format_occurrence_anchor(parsed), parsed
        date_str = original_start.get("date")
        if isinstance(date_str, str) and date_str:
            parsed = _parse_iso_date(date_str)
            return ical_uid, _format_occurrence_anchor(parsed), parsed
    return ical_uid, "", None


def _extract_outlook_identity(payload: dict) -> tuple[str | None, str, datetime | None]:
    """Extract (iCalUId, occurrence_anchor, original_start_at) from Outlook payload."""
    ical_uid = payload.get("iCalUId")
    if not isinstance(ical_uid, str) or not ical_uid:
        return None, "", None
    # Outlook iCalUId is unique per occurrence — no anchor needed.
    return ical_uid, "", None


async def _fetch_google_event(
    client: httpx.AsyncClient,
    event_id: str,
) -> dict[str, Any] | None:
    """Fetch full event details from Google Calendar API."""
    endpoint = f"/calendar/v3/calendars/primary/events/{event_id}"
    for attempt in range(3):
        try:
            return await calendar_api_request(client, endpoint)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            if exc.response.status_code == 429:
                retry_after = exc.response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else float(attempt + 1)
                )
                await asyncio.sleep(delay)
                continue
            raise
    return None


async def _fetch_outlook_event(
    client: httpx.AsyncClient,
    event_id: str,
) -> dict[str, Any] | None:
    """Fetch full event details from Microsoft Graph API."""
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
                    await asyncio.sleep(delay)
                    continue
                raise
    return None


def _apply_hydration(
    event: CalendarEvent,
    details: dict[str, Any],
    shared_uid: str,
    provider: str,
    occurrence_anchor: str,
    original_start_at: datetime | None,
) -> None:
    """Update event with hydrated data."""
    updated_payload = dict(event.data_payload or {})
    updated_payload.update(details)
    event.data_payload = updated_payload
    event.external_id = shared_uid
    if hasattr(event, "provider"):
        event.provider = provider
    if hasattr(event, "occurrence_anchor"):
        event.occurrence_anchor = occurrence_anchor
    if hasattr(event, "original_start_at"):
        event.original_start_at = original_start_at


async def process_org(
    organisation_id: UUID,
    execute: bool,
    stats: HydrateStats,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        # Find all events missing shared UID
        stmt = select(CalendarEvent).where(col(CalendarEvent.organisation_id) == organisation_id)
        result = await session.execute(stmt)
        all_events = result.scalars().all()

        missing_events = [e for e in all_events if not _has_shared_uid(e.data_payload)]
        stats.total_missing = len(missing_events)

        if not missing_events:
            print("  No events missing shared UID")
            return

        print(f"  Found {len(missing_events)} event(s) missing shared UID")

        # For each missing event, find its integration via user_states
        missing_event_ids = [e.event_id for e in missing_events]
        us_stmt = select(CalendarEventUserState).where(
            col(CalendarEventUserState.event_id).in_(missing_event_ids)
        )
        us_result = await session.execute(us_stmt)
        user_states = us_result.scalars().all()

        # Map event_id → integration_id
        event_to_integration: dict[UUID, UUID] = {}
        for us in user_states:
            if us.event_id not in event_to_integration:
                event_to_integration[us.event_id] = us.integration_id

        # Load integrations we need
        integration_ids = set(event_to_integration.values())
        if not integration_ids:
            print("  No integrations found for missing events")
            stats.no_integration = len(missing_events)
            return

        int_stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.integration_id).in_(tuple(integration_ids))
        )
        int_result = await session.execute(int_stmt)
        integrations = {i.integration_id: i for i in int_result.scalars().all()}

        # Group events by integration for token reuse
        events_by_integration: dict[UUID, list[CalendarEvent]] = {}
        for event in missing_events:
            int_id = event_to_integration.get(event.event_id)
            if not int_id or int_id not in integrations:
                stats.no_integration += 1
                continue
            events_by_integration.setdefault(int_id, []).append(event)

        if not execute:
            for int_id, events in events_by_integration.items():
                integration = integrations[int_id]
                print(
                    f"  Integration {int_id} ({integration.integration_type}): "
                    f"{len(events)} event(s) to hydrate"
                )
            return

        # Hydrate events per integration
        nango = NangoClient()
        try:
            for int_id, events in events_by_integration.items():
                integration = integrations[int_id]
                connection_id = integration.connection_id
                if not connection_id:
                    print(f"  Integration {int_id}: no connection_id, skipping")
                    stats.no_integration += len(events)
                    continue

                int_type = (
                    IntegrationType(integration.integration_type)
                    if isinstance(integration.integration_type, str)
                    else integration.integration_type
                )

                # Resolve provider config key
                metadata = integration.connection_metadata or {}
                provider_config_key = metadata.get("provider_config_key")
                if not isinstance(provider_config_key, str) or not provider_config_key.strip():
                    from src.infra.config.nango_provider_mapping import to_nango_provider_key

                    provider_config_key = to_nango_provider_key(int_type)

                is_google = int_type in GOOGLE_TYPES
                is_outlook = int_type in OUTLOOK_TYPES
                provider_label = "google" if is_google else "outlook" if is_outlook else "unknown"

                print(
                    f"  Integration {int_id} ({provider_label}): hydrating {len(events)} event(s) "
                    f"via connection {connection_id} provider={provider_config_key}"
                )

                try:
                    if is_google:
                        token = await google_get_access_token(
                            nango, connection_id, provider_config_key
                        )
                    else:
                        token = await outlook_get_access_token(
                            nango, connection_id, provider_config_key
                        )
                except Exception as e:
                    print(f"    ERROR getting token: {e}")
                    stats.api_errors += len(events)
                    continue

                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30.0,
                ) as client:
                    for event in events:
                        provider_event_id = _get_provider_event_id(event)
                        if not provider_event_id:
                            print(f"    {event.event_id}: no provider event ID found, skipping")
                            stats.still_missing += 1
                            continue

                        try:
                            if is_google:
                                details = await _fetch_google_event(client, provider_event_id)
                            elif is_outlook:
                                details = await _fetch_outlook_event(client, provider_event_id)
                            else:
                                print(
                                    f"    {event.event_id}: unsupported provider {int_type}, skipping"
                                )
                                stats.still_missing += 1
                                continue
                        except Exception as e:
                            print(f"    {event.event_id}: API error: {e}")
                            stats.api_errors += 1
                            continue

                        if not details:
                            print(f"    {event.event_id}: not found in API (404)")
                            stats.still_missing += 1
                            continue

                        # Extract shared UID from response
                        if is_google:
                            shared_uid, anchor, orig_start = _extract_google_identity(details)
                            provider_name = "google"
                        else:
                            shared_uid, anchor, orig_start = _extract_outlook_identity(details)
                            provider_name = "outlook"

                        if not shared_uid:
                            print(f"    {event.event_id}: API response still has no shared UID")
                            stats.still_missing += 1
                            continue

                        _apply_hydration(
                            event, details, shared_uid, provider_name, anchor, orig_start
                        )
                        session.add(event)
                        stats.hydrated += 1
                        print(f"    {event.event_id}: hydrated → {shared_uid[:50]}...")

                        # Batch commit every 50 events
                        if stats.hydrated % 50 == 0:
                            await session.commit()

        finally:
            await nango.close()

        await session.commit()
        print(
            f"  Done: hydrated={stats.hydrated} still_missing={stats.still_missing} "
            f"api_errors={stats.api_errors} no_integration={stats.no_integration}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hydrate calendar events missing shared UID by fetching from provider API"
    )
    parser.add_argument("--execute", action="store_true", help="Persist changes (default: dry run)")
    parser.add_argument(
        "--org-id",
        required=True,
        type=lambda v: str(UUID(v)),
        help="Organisation UUID to process",
    )
    args = parser.parse_args()

    org_id = UUID(args.org_id)
    stats = HydrateStats()
    mode = "EXECUTE" if args.execute else "DRY RUN"

    print("=" * 90)
    print(f"  HYDRATE MISSING SHARED UID — {mode}")
    print(f"  Org: {org_id}")
    print("=" * 90)

    await process_org(org_id, args.execute, stats)

    print()
    print("=" * 90)
    print(
        f"TOTAL: {stats.total_missing} missing, {stats.hydrated} hydrated, "
        f"{stats.still_missing} still missing, {stats.api_errors} API errors, "
        f"{stats.no_integration} no integration"
    )
    if not args.execute:
        print("DRY RUN — no changes made. Run with --execute to apply.")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
