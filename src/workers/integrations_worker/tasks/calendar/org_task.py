"""Tier 2: Organization-level calendar sync task.

Syncs all active calendar integrations for one org. Dispatched by
sync_calendar_all_orgs_task (Tier 1), calls provider APIs inline (not via
sync_integration_data_task) because calendar sync needs cursor management
that doesn't fit the generic dispatch pattern.

Uses the fresh-session pattern: extract integration metadata → release DB
connection → call external APIs → acquire fresh session to persist. This
avoids holding a connection during potentially slow provider calls (Google
Calendar / Outlook Graph can take 5-30s per page).
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
    """Snapshot of integration state needed for external API calls.

    Extracted from the DB so the session can be released before slow
    provider fetches. Fields are intentionally plain types (not ORM models)
    to avoid detached-instance errors outside the session scope.
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


CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


def _resolve_provider_config_key(
    *,
    integration_type: IntegrationType,
    connection_metadata: dict[str, Any] | None,
) -> str:
    """Resolve Nango provider config key, preferring metadata over defaults.

    Nango provider keys differ across environments (e.g. Google Calendar uses
    `google-calendar` in some envs). Metadata-first lets per-connection
    overrides win without environment-specific branching.
    """
    metadata = connection_metadata or {}
    metadata_key = metadata.get("provider_config_key")

    if integration_type == IntegrationType.GOOGLE_CALENDAR:
        normalized_metadata_key = metadata_key.strip() if isinstance(metadata_key, str) else ""
        if normalized_metadata_key in {"google-calendar", "google_calendar"}:
            return "google-calendar"
        return to_nango_connect_integration_key(integration_type)

    if isinstance(metadata_key, str) and metadata_key.strip():
        return metadata_key.strip()

    return to_nango_provider_key(integration_type)


@app.task(
    name="integrations.sync_calendar_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    # acks_late: if the worker crashes mid-sync, the message returns to the queue
    # and another worker retries — prevents silent data loss on OOM or deploy.
    acks_late=True,
    soft_time_limit=600,
    time_limit=660,
)
def sync_calendar_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Sync all calendar integrations for one organization.

    Triggered by sync_calendar_all_orgs_task (Tier 1). Iterates each active
    calendar integration, fetches events from the provider, and persists them.
    On success, dispatches deal-matching to link new events to deals.

    Args:
        organisation_id: The organization UUID string.

    Returns:
        Dict with status, summary counts, and per-integration results.
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _sync_org() -> dict[str, Any]:
        logger.info("Starting calendar sync for organization (Tier 2): %s", org_id)

        # Session released after this block — extract into plain dataclass so
        # the connection returns to the pool before slow provider API calls.
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

            for integration_row in integrations:
                connection_id = integration_row.connection_id
                # Not redundant with the isnot(None) filter above — catches empty
                # strings which pass IS NOT NULL but are unusable as connection IDs.
                if not connection_id:
                    logger.warning(
                        "Skipping integration with no connection_id: %s",
                        integration_row.integration_id,
                    )
                    continue

                # SQLModel sometimes returns the raw string instead of the enum
                # instance depending on driver/session state — normalize here.
                int_type = (
                    IntegrationType(integration_row.integration_type)
                    if isinstance(integration_row.integration_type, str)
                    else integration_row.integration_type
                )

                # Get Integration type from providers
                _, provider = categorize_integration_type(int_type)
                provider_config_key = _resolve_provider_config_key(
                    integration_type=int_type,
                    connection_metadata=integration_row.connection_metadata,
                )

                # Only fetch cursor on incremental syncs — initial syncs use time-bounded window
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
                    # exhaustion when Google/Outlook APIs are slow.
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

                    # Fresh session for persist — re-query integration because
                    # the Phase 1 instance is detached from any session.
                    async for session in acquire_org_async_session(org_id):
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

                        sync_result = await upsert_calendar_incremental(
                            session=session,
                            organisation_id=org_id,
                            integration=integration_db,
                            provider=fetch_data.provider,
                            events=events,
                        )
                        records_synced = sync_result.get("records_processed", 0)

                        # Save cursor only after successful persist — if we crash
                        # before commit, the next sync re-fetches from the old cursor.
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

                        # Strip tzinfo — column is `timestamp without time zone`; asyncpg
                        # rejects tz-aware datetimes for tz-naive columns.
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
                                "sync_breakdown": sync_result,
                            }
                        )
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

        # Trigger deal matching only when new events were persisted — avoids
        # unnecessary deal_worker load on no-op syncs.
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
