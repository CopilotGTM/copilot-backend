"""
Tier 2: Organization-level calendar sync task.

Syncs all calendar integrations for a specific organization.

This task:
1. Queries org DB for all active calendar integrations
2. For each integration, fetches events and syncs using CalendarSyncService
3. Returns org-level aggregated results

Architecture:
    sync_calendar_all_orgs_task [Tier 1]
        |
    sync_calendar_org_task [Tier 2 - this file] (one per org)
        |
    CalendarSyncService (for each integration, inline)

NOTE: This task calls the sync service directly (similar to CRM/Email pattern),
not via sync_integration_data_task dispatch.

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Query integrations, extract needed data, release session
    2. Fetch events from external APIs (NO session held)
    3. Session 2: Re-query integration, upsert events, update metadata, release
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.config.nango_provider_mapping import (
    to_nango_connect_integration_key,
    to_nango_provider_key,
)
from src.infra.config.provider_categories import categorize_integration_type
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.calendar_cursor_metadata import (
    get_calendar_delta_link,
    set_calendar_delta_link,
)
from src.workers.integrations_worker.adapters.calendar_adapter import upsert_calendar_incremental
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.calendar.google_calendar import (
    FetchEventsResult as GoogleFetchEventsResult,
)
from src.workers.integrations_worker.providers.calendar.google_calendar import (
    fetch_events_with_cursor as google_fetch_events,
)
from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
    FetchEventsResult as OutlookFetchEventsResult,
)
from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
    fetch_events as outlook_fetch_events,
)

logger = get_logger(__name__)


@dataclass
class IntegrationFetchData:
    """Data extracted from integration needed for fetching events.

    This dataclass holds the minimal data needed to fetch events from
    external APIs, allowing us to release the database session before
    making potentially long-running API calls.
    """

    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    integration_type_value: str
    provider: str
    provider_config_key: str
    is_initial: bool
    last_synced_at: datetime | None
    calendar_delta_link: str | None  # Outlook deltaLink or Google syncToken


# Calendar integration types to sync
CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


def _resolve_provider_config_key(
    *,
    integration_type: IntegrationType,
    connection_metadata: dict[str, Any] | None,
) -> str:
    """Resolve provider config key with metadata-first strategy.

    Why this exists:
    - Nango provider config keys can differ across environments.
    - For Google Calendar, some environments use
      `google-calendar-getting-started` as the provider key used by proxy calls.
    """
    metadata = connection_metadata or {}
    metadata_key = metadata.get("provider_config_key")
    if isinstance(metadata_key, str) and metadata_key.strip():
        return metadata_key.strip()

    if integration_type == IntegrationType.GOOGLE_CALENDAR:
        return to_nango_connect_integration_key(integration_type)

    return to_nango_provider_key(integration_type)


@app.task(
    name="integrations.sync_calendar_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,  # 2 minutes between retries
    acks_late=True,
    soft_time_limit=600,  # 10 minutes soft limit
    time_limit=660,  # 11 minutes hard limit
)
def sync_calendar_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Tier 2: Sync all calendar integrations for one organization.

    Triggered by: sync_calendar_all_orgs_task (Tier 1)

    This task loops through all active calendar integrations for the org
    and syncs calendar events using CalendarSyncService.

    Args:
        organisation_id: The organization UUID string

    Returns:
        Dictionary with org-level sync results:
        {
            "status": "completed",
            "organisation_id": "uuid",
            "started_at": "...",
            "completed_at": "...",
            "summary": {
                "total_integrations": 2,
                "successful": 2,
                "failed": 0,
                "total_events_synced": 150
            },
            "integration_results": [...]
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        """Sync all calendar integrations for the organization.

        Uses fresh session pattern:
        1. Session 1: Query integrations, extract data, release session
        2. Fetch events from external APIs (NO session held)
        3. Session 2: Re-query integration, upsert events, update metadata
        """
        logger.info("Starting calendar sync for organization (Tier 2): %s", org_id)

        # ─────────────────────────────────────────────────────────────────────
        # Phase 1: Query integrations and extract data needed for fetching
        # Session is released after this block
        # ─────────────────────────────────────────────────────────────────────
        integration_fetch_list: list[IntegrationFetchData] = []

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_type).in_(CALENDAR_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
                logger.info("No active calendar integrations for org: %s", org_id)
                return {
                    "status": "completed",
                    "organisation_id": organisation_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "summary": {
                        "total_integrations": 0,
                        "successful": 0,
                        "failed": 0,
                        "total_events_synced": 0,
                    },
                    "integration_results": [],
                }

            logger.info(
                "Found %d active calendar integration(s) for org: %s",
                len(integrations),
                org_id,
            )

            # Extract data needed for fetching (to release session before API calls)
            for integration_row in integrations:
                connection_id = integration_row.connection_id
                if not connection_id:
                    logger.warning(
                        "Skipping integration with no connection_id: %s",
                        integration_row.integration_id,
                    )
                    continue

                int_type = (
                    IntegrationType(integration_row.integration_type)
                    if isinstance(integration_row.integration_type, str)
                    else integration_row.integration_type
                )
                _, provider = categorize_integration_type(int_type)
                provider_config_key = _resolve_provider_config_key(
                    integration_type=int_type,
                    connection_metadata=integration_row.connection_metadata,
                )

                # Single shared calendar cursor key per integration row.
                connection_metadata = integration_row.connection_metadata or {}
                calendar_delta_link = None
                if integration_row.last_synced_at is not None:
                    calendar_delta_link = get_calendar_delta_link(connection_metadata, int_type)

                integration_fetch_list.append(
                    IntegrationFetchData(
                        integration_id=integration_row.integration_id,
                        connection_id=connection_id,
                        integration_type=int_type,
                        integration_type_value=int_type.value,
                        provider=provider,
                        provider_config_key=provider_config_key,
                        is_initial=integration_row.last_synced_at is None,
                        last_synced_at=integration_row.last_synced_at,
                        calendar_delta_link=calendar_delta_link,
                    )
                )
        # Session is now released

        if not integration_fetch_list:
            return {
                "status": "completed",
                "organisation_id": organisation_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "total_integrations": 0,
                    "successful": 0,
                    "failed": 0,
                    "total_events_synced": 0,
                },
                "integration_results": [],
            }

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2 & 3: For each integration, fetch events then persist
        # External API calls happen WITHOUT holding a database session
        # ─────────────────────────────────────────────────────────────────────
        integration_results: list[dict[str, Any]] = []
        successful = 0
        failed = 0
        total_events = 0

        nango = NangoClient()
        try:
            for fetch_data in integration_fetch_list:
                logger.info(
                    "Syncing calendar integration: org=%s integration=%s type=%s",
                    org_id,
                    fetch_data.integration_id,
                    fetch_data.integration_type_value,
                )

                try:
                    # ─────────────────────────────────────────────────────────
                    # Phase 2: Fetch events from external API (NO session held)
                    # ─────────────────────────────────────────────────────────
                    events: list[dict[str, Any]]
                    new_calendar_delta_link: str | None = None

                    if fetch_data.integration_type == IntegrationType.GOOGLE_CALENDAR:
                        google_fetch_result: GoogleFetchEventsResult = await google_fetch_events(
                            nango=nango,
                            connection_id=fetch_data.connection_id,
                            is_initial=fetch_data.is_initial,
                            last_synced_at=fetch_data.last_synced_at,
                            sync_token=fetch_data.calendar_delta_link,
                            provider_config_key=fetch_data.provider_config_key,
                        )
                        events = google_fetch_result.events
                        new_calendar_delta_link = google_fetch_result.sync_token
                    elif fetch_data.integration_type == IntegrationType.OUTLOOK_CALENDAR:
                        outlook_fetch_result: OutlookFetchEventsResult = await outlook_fetch_events(
                            nango=nango,
                            connection_id=fetch_data.connection_id,
                            is_initial=fetch_data.is_initial,
                            last_synced_at=fetch_data.last_synced_at,
                            provider_config_key=fetch_data.provider_config_key,
                            delta_link=fetch_data.calendar_delta_link,
                        )
                        events = outlook_fetch_result.events
                        new_calendar_delta_link = outlook_fetch_result.delta_link
                    else:
                        logger.warning(
                            "Unknown calendar provider: %s",
                            fetch_data.integration_type,
                        )
                        continue

                    logger.info(
                        "Fetched %d events for integration %s",
                        len(events),
                        fetch_data.integration_id,
                    )

                    # ─────────────────────────────────────────────────────────
                    # Phase 3: Persist to database (acquire fresh session)
                    # ─────────────────────────────────────────────────────────
                    async for session in acquire_org_async_session(org_id):
                        # Re-query integration to get fresh instance attached to session
                        stmt = select(KnowledgeIntegration).where(
                            col(KnowledgeIntegration.integration_id) == fetch_data.integration_id,
                        )
                        result = await session.execute(stmt)
                        integration_db = result.scalar_one_or_none()

                        if integration_db is None:
                            logger.warning(
                                "Integration no longer exists: %s",
                                fetch_data.integration_id,
                            )
                            failed += 1
                            integration_results.append(
                                {
                                    "integration_id": str(fetch_data.integration_id),
                                    "integration_type": fetch_data.integration_type_value,
                                    "status": "failed",
                                    "error": "Integration no longer exists",
                                }
                            )
                            continue

                        # Sync events to database
                        records_synced = await upsert_calendar_incremental(
                            session=session,
                            organisation_id=org_id,
                            integration=integration_db,
                            provider=fetch_data.provider,
                            events=events,
                        )

                        # Persist cursor after successful fetch+persist.
                        updated_metadata = dict(integration_db.connection_metadata or {})
                        if new_calendar_delta_link:
                            updated_metadata = set_calendar_delta_link(
                                updated_metadata,
                                fetch_data.integration_type,
                                new_calendar_delta_link,
                            )
                            logger.info(
                                "Calendar cursor saved (integration=%s provider=%s)",
                                fetch_data.integration_id,
                                fetch_data.integration_type_value,
                            )
                        integration_db.connection_metadata = updated_metadata

                        # Update last_synced_at
                        integration_db.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
                        session.add(integration_db)
                        await session.commit()

                        successful += 1
                        total_events += records_synced

                        integration_results.append(
                            {
                                "integration_id": str(fetch_data.integration_id),
                                "integration_type": fetch_data.integration_type_value,
                                "status": "success",
                                "events_synced": records_synced,
                            }
                        )
                    # Session released after this block

                except Exception as e:
                    logger.error(
                        "Failed to sync calendar integration: org=%s integration=%s error=%s",
                        org_id,
                        fetch_data.integration_id,
                        str(e),
                        exc_info=True,
                    )
                    failed += 1
                    integration_results.append(
                        {
                            "integration_id": str(fetch_data.integration_id),
                            "integration_type": fetch_data.integration_type_value,
                            "status": "failed",
                            "error": str(e),
                        }
                    )
        finally:
            await nango.close()

        completed_at = datetime.now(UTC).isoformat()

        # Determine overall status
        if failed == 0:
            status = "completed"
        elif successful == 0:
            status = "failed"
        else:
            status = "partial"

        logger.info(
            "Calendar sync complete for org %s: successful=%d failed=%d events=%d",
            org_id,
            successful,
            failed,
            total_events,
        )

        # Dispatch Task 2: Match calendar events to deals
        if total_events > 0:
            app.send_task(
                "deal.match_calendar_to_deals_org",
                args=[organisation_id],
                queue="deal",
            )
            logger.info("Dispatched calendar matching task for org %s", org_id)

        return {
            "status": status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "total_integrations": len(integration_fetch_list),
                "successful": successful,
                "failed": failed,
                "total_events_synced": total_events,
            },
            "integration_results": integration_results,
        }

    try:
        return run_async(_sync_org())
    except Exception as e:
        logger.error(
            "Calendar org sync failed: org=%s error=%s",
            organisation_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying calendar org sync: org=%s attempt=%d/%d",
                organisation_id,
                self.request.retries + 1,
                self.max_retries,
            )
            raise self.retry(exc=e) from e

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
