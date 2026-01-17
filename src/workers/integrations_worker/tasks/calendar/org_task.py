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
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select

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

# Calendar integration types to sync
CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


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
        """Sync all calendar integrations for the organization."""
        logger.info("Starting calendar sync for organization (Tier 2): %s", org_id)

        nango = NangoClient()
        try:
            async for session in acquire_org_async_session(org_id):
                # Query all active calendar integrations
                stmt = select(KnowledgeIntegration).where(
                    KnowledgeIntegration.integration_type.in_(CALENDAR_INTEGRATION_TYPES),  # type: ignore
                    KnowledgeIntegration.is_active == True,  # noqa: E712 # type: ignore
                    KnowledgeIntegration.connection_id.isnot(None),  # type: ignore
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

                # Sync each integration
                integration_results = []
                successful = 0
                failed = 0
                total_events = 0

                for integration in integrations:
                    integration_id = integration.integration_id
                    connection_id = integration.connection_id
                    if not connection_id:
                        logger.warning(
                            "Skipping integration with no connection_id: %s",
                            integration_id,
                        )
                        continue

                    # Get provider details
                    int_type = (
                        IntegrationType(integration.integration_type)
                        if isinstance(integration.integration_type, str)
                        else integration.integration_type
                    )
                    integration_type = int_type.value
                    _, provider = categorize_integration_type(int_type)
                    provider_config_key = to_nango_provider_key(int_type)

                    logger.info(
                        "Syncing calendar integration: org=%s integration=%s type=%s",
                        org_id,
                        integration_id,
                        integration_type,
                    )

                    try:
                        # Determine if this is initial sync
                        is_initial = integration.last_synced_at is None

                        # Fetch events based on provider
                        if int_type == IntegrationType.GOOGLE_CALENDAR:
                            events = await google_fetch_events(
                                nango=nango,
                                connection_id=connection_id,
                                is_initial=is_initial,
                                last_synced_at=integration.last_synced_at,
                                provider_config_key=provider_config_key,
                            )
                            new_delta_link = None
                        elif int_type == IntegrationType.OUTLOOK_CALENDAR:
                            # Get stored delta link for true incremental sync
                            stored_delta_link = (integration.connection_metadata or {}).get(
                                "outlook_delta_link"
                            )
                            fetch_result: FetchEventsResult = await outlook_fetch_events(
                                nango=nango,
                                connection_id=connection_id,
                                is_initial=is_initial,
                                last_synced_at=integration.last_synced_at,
                                provider_config_key=provider_config_key,
                                delta_link=stored_delta_link if not is_initial else None,
                            )
                            events = fetch_result.events
                            new_delta_link = fetch_result.delta_link
                        else:
                            logger.warning(
                                "Unknown calendar provider: %s",
                                int_type,
                            )
                            continue

                        logger.info(
                            "Fetched %d events for integration %s",
                            len(events),
                            integration_id,
                        )

                        # Sync events to database
                        records_synced = await upsert_calendar_incremental(
                            session=session,
                            organisation_id=org_id,
                            integration=integration,
                            provider=provider,
                            events=events,
                        )

                        # Save new deltaLink for Outlook (enables true incremental sync)
                        if new_delta_link and int_type == IntegrationType.OUTLOOK_CALENDAR:
                            connection_metadata = dict(integration.connection_metadata or {})
                            connection_metadata["outlook_delta_link"] = new_delta_link
                            integration.connection_metadata = connection_metadata
                            session.add(integration)
                            await session.commit()
                            logger.info(
                                "Outlook calendar: saved new deltaLink for next sync (integration=%s)",
                                integration_id,
                            )

                        # Update last_synced_at
                        integration.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
                        session.add(integration)
                        await session.commit()

                        successful += 1
                        total_events += records_synced

                        integration_results.append(
                            {
                                "integration_id": str(integration_id),
                                "integration_type": integration_type,
                                "status": "success",
                                "events_synced": records_synced,
                            }
                        )

                    except Exception as e:
                        logger.error(
                            "Failed to sync calendar integration: org=%s integration=%s error=%s",
                            org_id,
                            integration_id,
                            str(e),
                            exc_info=True,
                        )
                        failed += 1
                        integration_results.append(
                            {
                                "integration_id": str(integration_id),
                                "integration_type": integration_type,
                                "status": "failed",
                                "error": str(e),
                            }
                        )

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
                    from src.workers.deal_worker.tasks.deal_mappers import (
                        match_calendar_to_deals_org_task,
                    )

                    match_calendar_to_deals_org_task.delay(organisation_id)  # type: ignore
                    logger.info("Dispatched calendar matching task for org %s", org_id)

                return {
                    "status": status,
                    "organisation_id": organisation_id,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "summary": {
                        "total_integrations": len(integrations),
                        "successful": successful,
                        "failed": failed,
                        "total_events_synced": total_events,
                    },
                    "integration_results": integration_results,
                }
        finally:
            await nango.close()

        # If async for didn't yield any sessions
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
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
