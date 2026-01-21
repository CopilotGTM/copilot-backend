"""
Tier 3: Integration-level calendar sync task.

Syncs a specific calendar integration for manual/user-level triggers.

This task is used for:
- Frontend "Refresh" button on integrations page
- API endpoint for on-demand sync
- Manual intervention/debugging

NOTE: This task is NOT used by the scheduled sync (Celery Beat).
The scheduler uses Tier 1 -> Tier 2, which handles integrations inline.

Architecture:
    Frontend/API (manual trigger)
        |
    sync_calendar_integration_task [Tier 3 - this file]
        |
    CalendarSyncService (direct call, same pattern as CRM/Email)

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections during long-running external API calls:
    1. Session 1: Query integration, validate, extract needed data, release session
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
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.config.provider_categories import categorize_integration_type
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.workers.integrations_worker.adapters.calendar_adapter import upsert_calendar_incremental
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.providers.calendar.google_calendar import (
    fetch_events as google_fetch_events,
)
from src.workers.integrations_worker.providers.calendar.outlook_calendar import (
    FetchEventsResult,
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
    delta_link: str | None  # For Outlook incremental sync


# Calendar integration types
CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


@app.task(
    name="integrations.sync_calendar_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute between retries
    acks_late=True,
    soft_time_limit=300,  # 5 minutes soft limit for single integration
    time_limit=360,  # 6 minutes hard limit
)
def sync_calendar_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """
    Tier 3: Sync specific calendar integration.

    Triggered by: Frontend refresh button, API, manual intervention
    NOT triggered by: Scheduled sync (Tier 1/2)

    This task is for on-demand/manual sync of a single calendar integration.
    It provides immediate feedback to the user.

    Args:
        organisation_id: The organization UUID string
        integration_id: The specific integration UUID string

    Returns:
        Dictionary with integration sync results:
        {
            "status": "success",
            "organisation_id": "uuid",
            "integration_id": "uuid",
            "integration_type": "google_calendar",
            "started_at": "...",
            "completed_at": "...",
            "events_synced": 50
        }
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _sync_integration() -> dict[str, Any]:
        """Sync the specific calendar integration.

        Uses fresh session pattern:
        1. Session 1: Query integration, validate, extract data, release session
        2. Fetch events from external APIs (NO session held)
        3. Session 2: Re-query integration, upsert events, update metadata
        """
        logger.info(
            "Starting manual calendar sync (Tier 3): org=%s integration=%s",
            org_id,
            int_id,
        )

        # Phase 1: Query integration and extract data needed for fetching
        fetch_data: IntegrationFetchData | None = None

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == int_id,
                col(KnowledgeIntegration.integration_type).in_(CALENDAR_INTEGRATION_TYPES),
            )
            result = await session.execute(stmt)
            integration = result.scalar_one_or_none()

            if not integration:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration not found or not a calendar integration",
                }

            if not integration.is_active:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration is not active",
                }

            if not integration.connection_id:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration is not connected (no connection_id)",
                }

            # Get provider details
            int_type = (
                IntegrationType(integration.integration_type)
                if isinstance(integration.integration_type, str)
                else integration.integration_type
            )
            _, provider = categorize_integration_type(int_type)
            provider_config_key = to_nango_provider_key(int_type)

            # Extract delta link for Outlook
            delta_link = None
            if int_type == IntegrationType.OUTLOOK_CALENDAR:
                is_initial = integration.last_synced_at is None
                if not is_initial:
                    delta_link = (integration.connection_metadata or {}).get("outlook_delta_link")

            fetch_data = IntegrationFetchData(
                integration_id=integration.integration_id,
                connection_id=integration.connection_id,
                integration_type=int_type,
                integration_type_value=int_type.value,
                provider=provider,
                provider_config_key=provider_config_key,
                is_initial=integration.last_synced_at is None,
                last_synced_at=integration.last_synced_at,
                delta_link=delta_link,
            )
        # Session is now released

        if not fetch_data:
            return {
                "status": "failed",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "error": "Failed to acquire database session",
            }

        logger.info(
            "Syncing calendar integration: org=%s integration=%s type=%s",
            org_id,
            int_id,
            fetch_data.integration_type_value,
        )

        # Phase 2: Fetch events from external API (NO session held)
        events: list[dict[str, Any]]
        new_delta_link: str | None = None

        nango = NangoClient()
        try:
            if fetch_data.integration_type == IntegrationType.GOOGLE_CALENDAR:
                events = await google_fetch_events(
                    nango=nango,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                )
            elif fetch_data.integration_type == IntegrationType.OUTLOOK_CALENDAR:
                fetch_result: FetchEventsResult = await outlook_fetch_events(
                    nango=nango,
                    connection_id=fetch_data.connection_id,
                    is_initial=fetch_data.is_initial,
                    last_synced_at=fetch_data.last_synced_at,
                    provider_config_key=fetch_data.provider_config_key,
                    delta_link=fetch_data.delta_link,
                )
                events = fetch_result.events
                new_delta_link = fetch_result.delta_link
            else:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": f"Unknown calendar provider: {fetch_data.integration_type}",
                }
        finally:
            await nango.close()

        logger.info(
            "Fetched %d events for integration %s",
            len(events),
            int_id,
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 3: Persist to database (acquire fresh session)
        # ─────────────────────────────────────────────────────────────────────
        async for session in acquire_org_async_session(org_id):
            # Re-query integration to get fresh instance attached to session
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == int_id,
            )
            result = await session.execute(stmt)
            integration = result.scalar_one_or_none()

            if not integration:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration no longer exists",
                }

            # Sync events to database
            records_synced = await upsert_calendar_incremental(
                session=session,
                organisation_id=org_id,
                integration=integration,
                provider=fetch_data.provider,
                events=events,
            )

            # Save new deltaLink for Outlook
            if new_delta_link and fetch_data.integration_type == IntegrationType.OUTLOOK_CALENDAR:
                connection_metadata = dict(integration.connection_metadata or {})
                connection_metadata["outlook_delta_link"] = new_delta_link
                integration.connection_metadata = connection_metadata
                session.add(integration)
                await session.commit()
                logger.info(
                    "Outlook calendar: saved new deltaLink (integration=%s)",
                    int_id,
                )

            # Update last_synced_at
            integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(integration)
            await session.commit()

            logger.info(
                "Manual calendar sync complete: org=%s integration=%s events_synced=%d",
                org_id,
                int_id,
                records_synced,
            )

            return {
                "status": "success",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "integration_type": fetch_data.integration_type_value,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "events_synced": records_synced,
            }
        # Session released after this block

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session for persist phase",
        }

    try:
        result = run_async(_sync_integration())

        # Log based on status
        if result.get("status") == "success":
            logger.info(
                "Manual calendar sync succeeded: org=%s integration=%s events=%d",
                organisation_id,
                integration_id,
                result.get("events_synced", 0),
            )
        else:
            logger.warning(
                "Manual calendar sync failed: org=%s integration=%s error=%s",
                organisation_id,
                integration_id,
                result.get("error"),
            )

        return result

    except Exception as e:
        logger.error(
            "Manual calendar sync exception: org=%s integration=%s error=%s",
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )

        # Check if we should retry
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying manual calendar sync: org=%s integration=%s attempt=%d/%d",
                organisation_id,
                integration_id,
                self.request.retries + 1,
                self.max_retries,
            )
            raise self.retry(exc=e) from e

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }


# Convenience alias
sync_calendar_integration = sync_calendar_integration_task
