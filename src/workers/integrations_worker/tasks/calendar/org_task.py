"""Tier 2: Organization-level calendar sync task.

Syncs all active calendar integrations for one org. Dispatched by
sync_calendar_all_orgs_task (Tier 1), calls provider APIs inline (not via
sync_integration_data_task) because calendar sync needs cursor management
that doesn't fit the generic dispatch pattern.

Uses the fresh-session pattern: extract integration metadata → release DB
connection → call external APIs → acquire fresh session to persist. This
avoids holding a connection during potentially slow provider calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import (
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.provider_categories import categorize_integration_type
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.calendar_cursor_metadata import (
    get_calendar_delta_link,
    set_calendar_delta_link,
)
from src.service.integrations.calendar_sync_service import CalendarSyncService
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.sync_health import (
    CIRCUIT_BREAKER_THRESHOLD,
    apply_sync_success,
    classify_sync_error,
    is_action_required_failure,
    record_sync_failure,
)
from src.workers.deal_worker.services.deal_mapper_services.communication_deal_mapping_trigger_service import (
    CommunicationDealMappingTriggerService,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS
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
    is_initial: bool
    last_synced_at: datetime | None
    calendar_delta_link: str | None  # Google syncToken
    credentials: dict[str, Any]
    access_token_expires_at: datetime | None
    scopes: list[str] | None
    sync_state_id: UUID | None = None


CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)
CALENDAR_PARENT_TYPES = (IntegrationType.GOOGLE, IntegrationType.MICROSOFT)
_CALENDAR_CAPABILITY_FOR_PARENT: dict[IntegrationType, IntegrationType] = {
    IntegrationType.GOOGLE: IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.MICROSOFT: IntegrationType.OUTLOOK_CALENDAR,
}

_TASK_NAME = "integrations.sync_calendar_org"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 120),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_calendar_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """Sync all calendar integrations for one organization.

    Triggered by sync_calendar_all_orgs_task (Tier 1). Iterates each active
    calendar integration, fetches events from the provider, and persists them.
    Deal mapping is handled by initial backfill and event-based deal triggers.

    Args:
        organisation_id: The organization UUID string.

    Returns:
        Dict with status, summary counts, and per-integration results.
    """
    started_at_dt = datetime.now(UTC)
    started_at = started_at_dt.isoformat()
    org_id = UUID(organisation_id)
    current_integration_id: list[UUID | None] = [None]

    async def _sync_org() -> dict[str, Any]:
        logger.info(
            "Calendar sync starting for org (Tier 2)",
            extra={"organisation_id": organisation_id},
        )

        # Session released after this block — extract into plain dataclass so
        # the connection returns to the pool before slow provider API calls.
        integration_fetch_list: list[IntegrationFetchData] = []

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_type).in_(
                    CALENDAR_INTEGRATION_TYPES + CALENDAR_PARENT_TYPES
                ),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
                # Circuit breaker: skip integrations with too many consecutive
                # transient failures. Counter resets on success, so recovery
                # is automatic when the provider comes back.
                col(KnowledgeIntegration.consecutive_sync_failures) < CIRCUIT_BREAKER_THRESHOLD,
            )
            result = await session.execute(stmt)
            integrations = result.scalars().all()

            if not integrations:
                logger.info(
                    "No active calendar integrations for org",
                    extra={"organisation_id": organisation_id},
                )
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
                "Calendar sync found integrations for org",
                extra={
                    "organisation_id": organisation_id,
                    "total_integrations": len(integrations),
                },
            )

            for integration_row in integrations:
                connection_id = integration_row.connection_id
                # Not redundant with the isnot(None) filter above — catches empty
                # strings which pass IS NOT NULL but are unusable as connection IDs.
                if not connection_id:
                    logger.warning(
                        "Skipping integration with no connection_id",
                        extra={"integration_id": str(integration_row.integration_id)},
                    )
                    continue

                # SQLModel sometimes returns the raw string instead of the enum
                # instance depending on driver/session state — normalize here.
                int_type = (
                    IntegrationType(integration_row.integration_type)
                    if isinstance(integration_row.integration_type, str)
                    else integration_row.integration_type
                )

                sync_state_id = None
                cursor_metadata = dict(integration_row.connection_metadata or {})
                last_synced_at = integration_row.last_synced_at
                capability = _CALENDAR_CAPABILITY_FOR_PARENT.get(int_type)
                if capability:
                    state_stmt = select(IntegrationSyncState).where(
                        col(IntegrationSyncState.integration_id) == integration_row.integration_id,
                        col(IntegrationSyncState.capability) == capability,
                        col(IntegrationSyncState.enabled).is_(True),
                    )
                    state_result = await session.execute(state_stmt)
                    state = state_result.scalar_one_or_none()
                    if state is None:
                        logger.info(
                            "Skipping provider integration without enabled calendar capability",
                            extra={
                                "organisation_id": organisation_id,
                                "integration_id": str(integration_row.integration_id),
                                "provider_type": int_type.value,
                            },
                        )
                        continue
                    sync_state_id = state.sync_state_id
                    cursor_metadata = dict(state.cursor_metadata or {})
                    last_synced_at = state.last_synced_at
                    int_type = capability

                _, provider = categorize_integration_type(int_type)

                # Only fetch cursor on incremental syncs — initial syncs use time-bounded window
                calendar_delta_link = None
                if last_synced_at is not None:
                    calendar_delta_link = get_calendar_delta_link(cursor_metadata, int_type)
                    if calendar_delta_link is None:
                        logger.warning(
                            "Calendar incremental sync missing stored cursor",
                            extra={
                                "organisation_id": organisation_id,
                                "integration_id": str(integration_row.integration_id),
                                "integration_type": int_type.value,
                                "last_synced_at_present": True,
                            },
                        )

                integration_fetch_list.append(
                    IntegrationFetchData(
                        integration_id=integration_row.integration_id,
                        connection_id=connection_id,
                        integration_type=int_type,
                        integration_type_value=int_type.value,
                        provider=provider,
                        is_initial=last_synced_at is None,
                        last_synced_at=last_synced_at,
                        calendar_delta_link=calendar_delta_link,
                        credentials=dict(integration_row.credentials or {}),
                        access_token_expires_at=integration_row.access_token_expires_at,
                        scopes=list(integration_row.scopes or []),
                        sync_state_id=sync_state_id,
                    )
                )
        # Session released

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
        action_required = 0
        total_events = 0

        for fetch_data in integration_fetch_list:
            current_integration_id[0] = fetch_data.integration_id
            integration_sync_started_at = datetime.now(UTC)
            logger.info(
                "Syncing calendar integration",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": str(fetch_data.integration_id),
                    "integration_type": fetch_data.integration_type_value,
                },
            )

            try:
                events: list[dict[str, Any]]
                new_calendar_delta_link: str | None = None

                token_info = IntegrationTokenInfo.from_integration(fetch_data, org_id)

                if fetch_data.integration_type == IntegrationType.GOOGLE_CALENDAR:
                    google_fetch_result: GoogleFetchEventsResult = await google_fetch_events(
                        token_info=token_info,
                        connection_id=fetch_data.connection_id,
                        is_initial=fetch_data.is_initial,
                        last_synced_at=fetch_data.last_synced_at,
                        sync_token=fetch_data.calendar_delta_link,
                    )
                    events = google_fetch_result.events
                    new_calendar_delta_link = google_fetch_result.sync_token

                elif fetch_data.integration_type == IntegrationType.OUTLOOK_CALENDAR:
                    outlook_fetch_result: OutlookFetchEventsResult = await outlook_fetch_events(
                        token_info=token_info,
                        connection_id=fetch_data.connection_id,
                        is_initial=fetch_data.is_initial,
                        last_synced_at=fetch_data.last_synced_at,
                        delta_link=fetch_data.calendar_delta_link,
                    )
                    events = outlook_fetch_result.events
                    new_calendar_delta_link = outlook_fetch_result.delta_link
                else:
                    logger.warning(
                        "Unknown calendar provider",
                        extra={
                            "integration_id": str(fetch_data.integration_id),
                            "integration_type": str(fetch_data.integration_type),
                        },
                    )
                    continue

                logger.info(
                    "Calendar events fetched",
                    extra={
                        "integration_id": str(fetch_data.integration_id),
                        "provider": fetch_data.provider,
                        "events_fetched": len(events),
                        "cursor_present": fetch_data.calendar_delta_link is not None,
                        "cursor_used": (
                            not fetch_data.is_initial and fetch_data.calendar_delta_link is not None
                        ),
                        "cursor_returned": new_calendar_delta_link is not None,
                    },
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
                            "Integration no longer exists",
                            extra={"integration_id": str(fetch_data.integration_id)},
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

                    service = CalendarSyncService(session, org_id)
                    sync_result = await service.sync_calendar_events_incremental(
                        integration=integration_db,
                        provider=fetch_data.provider,
                        events=events,
                    )
                    records_synced = sync_result.get("records_processed", 0)
                    cursor_saved = new_calendar_delta_link is not None

                    state_db = None
                    if fetch_data.sync_state_id:
                        state_stmt = select(IntegrationSyncState).where(
                            col(IntegrationSyncState.sync_state_id) == fetch_data.sync_state_id,
                        )
                        state_result = await session.execute(state_stmt)
                        state_db = state_result.scalar_one_or_none()

                    # Save cursor only after successful persist — if we crash
                    # before commit, the next sync re-fetches from the old cursor.
                    if state_db is not None:
                        updated_metadata = dict(state_db.cursor_metadata or {})
                    else:
                        updated_metadata = dict(integration_db.connection_metadata or {})

                    if new_calendar_delta_link:
                        updated_metadata = set_calendar_delta_link(
                            updated_metadata,
                            fetch_data.integration_type,
                            new_calendar_delta_link,
                        )
                        logger.info(
                            "Calendar cursor saved",
                            extra={
                                "integration_id": str(fetch_data.integration_id),
                                "integration_type": fetch_data.integration_type_value,
                            },
                        )

                    if state_db is not None:
                        state_db.cursor_metadata = updated_metadata
                    else:
                        integration_db.connection_metadata = updated_metadata

                    logger.info(
                        "Calendar sync persistence diagnostics",
                        extra={
                            "organisation_id": organisation_id,
                            "integration_id": str(fetch_data.integration_id),
                            "provider": fetch_data.provider,
                            "events_fetched": len(events),
                            "upsert_candidate_count": sync_result.get("created", 0)
                            + sync_result.get("updated", 0),
                            "unchanged_count": sync_result.get("skipped", 0),
                            "cursor_present": fetch_data.calendar_delta_link is not None,
                            "cursor_used": (
                                not fetch_data.is_initial
                                and fetch_data.calendar_delta_link is not None
                            ),
                            "cursor_saved": cursor_saved,
                        },
                    )

                    # Strip tzinfo — column is `timestamp without time zone`; asyncpg
                    # rejects tz-aware datetimes for tz-naive columns.
                    synced_at = datetime.now(UTC).replace(tzinfo=None)
                    if state_db is not None:
                        state_db.last_synced_at = synced_at
                    else:
                        integration_db.last_synced_at = synced_at
                    if state_db is not None:
                        state_db.last_sync_status = "success"
                        state_db.last_sync_error = None
                        state_db.consecutive_sync_failures = 0
                        session.add(state_db)
                    else:
                        # Legacy rows keep sync health on the integration, while
                        # capability rows keep it on IntegrationSyncState.
                        apply_sync_success(integration_db)
                    session.add(integration_db)
                    await session.commit()

                    successful += 1
                    total_events += records_synced
                    communication_deal_mapping = None
                    if records_synced > 0 or sync_result.get("changed_event_ids"):
                        communication_deal_mapping = (
                            await CommunicationDealMappingTriggerService(
                                org_id
                            ).trigger_for_changed_communications(
                                integration_id=fetch_data.integration_id,
                                source_kind="calendar",
                                trigger_source="incremental_sync",
                                changed_record_ids=sync_result.get("changed_event_ids", []),
                                sync_started_at=integration_sync_started_at,
                            )
                        ).to_dict()

                    integration_results.append(
                        {
                            "integration_id": str(fetch_data.integration_id),
                            "integration_type": fetch_data.integration_type_value,
                            "status": "success",
                            "events_synced": records_synced,
                            "sync_breakdown": sync_result,
                            "communication_deal_mapping": communication_deal_mapping,
                        }
                    )
            except Exception as e:
                failure_type = classify_sync_error(e)
                log_extra = {
                    "organisation_id": organisation_id,
                    "integration_id": str(fetch_data.integration_id),
                    "error": str(e),
                    "failure_type": failure_type.value,
                }
                if is_action_required_failure(failure_type):
                    logger.warning(
                        "Calendar integration requires user action",
                        extra=log_extra,
                    )
                else:
                    logger.error(
                        "Failed to sync calendar integration",
                        exc_info=True,
                        extra=log_extra,
                    )

                await record_sync_failure(
                    organisation_id=org_id,
                    integration_id=fetch_data.integration_id,
                    error=e,
                    failure_type=failure_type,
                )

                if is_action_required_failure(failure_type):
                    action_required += 1
                else:
                    failed += 1
                integration_results.append(
                    {
                        "integration_id": str(fetch_data.integration_id),
                        "integration_type": fetch_data.integration_type_value,
                        "status": (
                            "action_required"
                            if is_action_required_failure(failure_type)
                            else "failed"
                        ),
                        "error": str(e),
                        "failure_type": failure_type.value,
                    }
                )

        completed_at = datetime.now(UTC).isoformat()

        if failed == 0:
            status = "completed"
        elif successful == 0:
            status = "failed"
        else:
            status = "partial"

        logger.info(
            "Calendar sync complete for org",
            extra={
                "organisation_id": organisation_id,
                "successful": successful,
                "failed": failed,
                "action_required": action_required,
                "total_events_synced": total_events,
            },
        )

        return {
            "status": status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "total_integrations": len(integration_fetch_list),
                "successful": successful,
                "failed": failed,
                "action_required": action_required,
                "total_events_synced": total_events,
            },
            "integration_results": integration_results,
        }

    try:
        return run_async(_sync_org())
    except SoftTimeLimitExceeded:
        logger.error(
            "Calendar org sync timed out",
            extra={"organisation_id": organisation_id, "soft_limit_seconds": _L["soft"]},
        )
        int_id = current_integration_id[0]
        if int_id:
            try:
                run_async(
                    record_sync_failure(
                        organisation_id=org_id,
                        integration_id=int_id,
                        error=TimeoutError(f"Soft time limit exceeded ({_L['soft']}s)"),
                    )
                )
            except Exception:
                logger.warning(
                    "Failed to record timeout failure",
                    extra={"integration_id": str(int_id)},
                )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_L['soft']}s",
        }
    except Exception as e:
        logger.error(
            "Calendar org sync failed",
            exc_info=True,
            extra={"organisation_id": organisation_id, "error": str(e)},
        )

        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying calendar org sync",
                extra={
                    "organisation_id": organisation_id,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
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
