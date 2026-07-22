"""Tier 3: Integration-level calendar sync task.

Syncs a specific calendar integration for manual and webhook triggers.
Uses custom OAuth token management (not Nango) via IntegrationTokenInfo.

This task is used for:
- Frontend "Refresh" button on integrations page
- API endpoint for on-demand sync
- Manual intervention/debugging

The webhook recovery watchdog may replay this task, but it does not perform
full scheduled polling; provider cursors remain the source of truth.

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
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import (
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
    SyncStatus,
)
from src.infra.config.provider_categories import categorize_integration_type
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.calendar_cursor_metadata import (
    apply_calendar_sync_outcome,
    get_calendar_delta_link,
)
from src.service.integrations.calendar_sync_service import CalendarSyncService
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.sync_health import (
    classify_sync_error,
    is_action_required_failure,
    record_sync_failure,
)
from src.service.integrations.webhooks.sync_receipt import (
    is_terminal_webhook_result,
    update_webhook_sync_receipt,
)
from src.workers.deal_worker.services.deal_mapper_services.communication_deal_mapping_trigger_service import (
    CommunicationDealMappingTriggerService,
)
from src.workers.deal_worker.utils.contact_communication_hydration_dispatch import (
    dispatch_contact_communication_hydration_async,
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
    is_initial: bool
    last_synced_at: datetime | None
    calendar_delta_link: str | None  # Outlook deltaLink or Google syncToken
    credentials: dict[str, Any]
    access_token_expires_at: datetime | None
    scopes: list[str] | None
    sync_state_id: UUID | None = None


# Calendar integration types
CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)
CALENDAR_PARENT_TYPES = (IntegrationType.GOOGLE, IntegrationType.MICROSOFT)
_CALENDAR_CAPABILITY_FOR_PARENT: dict[IntegrationType, IntegrationType] = {
    IntegrationType.GOOGLE: IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.MICROSOFT: IntegrationType.OUTLOOK_CALENDAR,
}

_TASK_NAME = "integrations.sync_calendar_integration"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_calendar_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
    trigger_source: str = "manual_sync",
    webhook_sync_id: str | None = None,
) -> dict[str, Any]:
    """
    Tier 3: Sync specific calendar integration.

    Triggered by: Frontend refresh button, API, webhook delivery/recovery

    This task syncs one calendar integration and is safe for webhook redelivery.
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
    started_at_dt = datetime.now(UTC)
    started_at = started_at_dt.isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)
    current_capability: list[IntegrationType | None] = [None]
    communication_trigger_source = "webhook" if trigger_source == "webhook" else "manual_sync"
    receipt_id = UUID(webhook_sync_id) if webhook_sync_id else None

    def _record_receipt(
        status: SyncStatus,
        *,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> None:
        """Update the durable ingress row without changing manual-sync behavior."""
        if receipt_id is None:
            return
        run_async(
            update_webhook_sync_receipt(
                organisation_id=org_id,
                sync_id=receipt_id,
                status=status,
                metadata=metadata,
                error=error,
                completed=completed,
            )
        )

    # Calendar writes are keyed by provider identities and advance the cursor
    # only after persistence, so broker/task redelivery is intentionally safe.
    _record_receipt(
        SyncStatus.IN_PROGRESS,
        metadata={"sync_attempt": self.request.retries + 1},
    )

    async def _sync_integration() -> dict[str, Any]:
        """Sync the specific calendar integration.

        Uses fresh session pattern:
        1. Session 1: Query integration, validate, extract data, release session
        2. Fetch events from external APIs (NO session held)
        3. Session 2: Re-query integration, upsert events, update metadata
        """
        logger.info(
            "Manual calendar sync starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        # Phase 1: Query integration and extract data needed for fetching
        fetch_data: IntegrationFetchData | None = None

        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == int_id,
                col(KnowledgeIntegration.integration_type).in_(
                    CALENDAR_INTEGRATION_TYPES + CALENDAR_PARENT_TYPES
                ),
            )
            result = await session.execute(stmt)
            integration_row = result.scalar_one_or_none()

            if integration_row is None:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration not found or not a calendar integration",
                }

            if not integration_row.is_active:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration is not active",
                }

            if not integration_row.connection_id:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration is not connected (no connection_id)",
                }

            # Parent provider rows store calendar cursor state on the capability row;
            # direct calendar integrations keep it on the integration itself.
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
                    return {
                        "status": "failed",
                        "organisation_id": organisation_id,
                        "integration_id": integration_id,
                        "started_at": started_at,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "error": "Calendar capability is not enabled",
                    }
                cursor_metadata = dict(state.cursor_metadata or {})
                last_synced_at = state.last_synced_at
                sync_state_id = state.sync_state_id
                int_type = capability
            _, provider = categorize_integration_type(int_type)
            current_capability[0] = int_type

            # Single shared calendar cursor key per integration row.
            calendar_delta_link = None
            if last_synced_at is not None:
                calendar_delta_link = get_calendar_delta_link(cursor_metadata, int_type)
                if calendar_delta_link is None:
                    logger.warning(
                        "Calendar incremental sync missing stored cursor",
                        extra={
                            "integration_id": integration_id,
                            "integration_type": int_type.value,
                            "last_synced_at_present": True,
                        },
                    )

            fetch_data = IntegrationFetchData(
                integration_id=integration_row.integration_id,
                connection_id=integration_row.connection_id,
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
            "Syncing calendar integration",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "integration_type": fetch_data.integration_type_value,
            },
        )

        # Phase 2: Fetch events from external API (NO session held)
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
            return {
                "status": "failed",
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "error": f"Unknown calendar provider: {fetch_data.integration_type}",
            }

        logger.info(
            "Calendar events fetched",
            extra={
                "integration_id": integration_id,
                "provider": fetch_data.provider,
                "events_fetched": len(events),
                "cursor_present": fetch_data.calendar_delta_link is not None,
                "cursor_used": (
                    not fetch_data.is_initial and fetch_data.calendar_delta_link is not None
                ),
                "cursor_returned": new_calendar_delta_link is not None,
            },
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
            integration_db = result.scalar_one_or_none()

            if integration_db is None:
                return {
                    "status": "failed",
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": "Integration no longer exists",
                }

            # Sync events to database
            service = CalendarSyncService(session, org_id)
            sync_result = await service.sync_calendar_events_incremental(
                integration=integration_db,
                provider=fetch_data.provider,
                events=events,
            )
            records_synced = sync_result.get("records_processed", 0)
            sync_status = sync_result.get("status", "failed")
            state = None
            if fetch_data.sync_state_id:
                state_stmt = select(IntegrationSyncState).where(
                    col(IntegrationSyncState.sync_state_id) == fetch_data.sync_state_id,
                )
                state_result = await session.execute(state_stmt)
                state = state_result.scalar_one_or_none()

            # Both manual and scheduled paths use this policy. Partial pages
            # replay twice, then quarantine bounded diagnostics so one poison
            # event cannot stop later provider changes indefinitely.
            persistence = apply_calendar_sync_outcome(
                integration=integration_db,
                state=state,
                integration_type=fetch_data.integration_type,
                sync_result=sync_result,
                input_cursor=fetch_data.calendar_delta_link,
                next_cursor=new_calendar_delta_link,
            )
            cursor_saved = persistence.cursor_advanced
            sync_result["cursor_quarantined"] = persistence.quarantined
            sync_result["partial_attempts"] = persistence.partial_attempts
            if state is not None:
                session.add(state)
            if cursor_saved:
                logger.info(
                    "Calendar cursor saved",
                    extra={
                        "integration_id": integration_id,
                        "integration_type": fetch_data.integration_type_value,
                    },
                )
            logger.info(
                "Calendar sync persistence diagnostics",
                extra={
                    "integration_id": integration_id,
                    "provider": fetch_data.provider,
                    "events_fetched": len(events),
                    "upsert_candidate_count": sync_result.get("created", 0)
                    + sync_result.get("updated", 0),
                    "unchanged_count": sync_result.get("skipped", 0),
                    "cursor_present": fetch_data.calendar_delta_link is not None,
                    "cursor_used": (
                        not fetch_data.is_initial and fetch_data.calendar_delta_link is not None
                    ),
                    "cursor_saved": cursor_saved,
                },
            )

            session.add(integration_db)
            await session.commit()

            communication_deal_mapping = None
            if records_synced > 0 or sync_result.get("changed_event_ids"):
                # Both follow-ups read committed calendar rows in independent worker sessions.
                # Enqueue hydration after the commit above, before deal mapping may replace a
                # direct activity card with its association-backed version.
                sync_result[
                    "contact_communication_hydration_task_ids"
                ] = await dispatch_contact_communication_hydration_async(
                    organisation_id=org_id,
                    integration_id=int_id,
                    changed_event_ids=sync_result.get("changed_event_ids", []),
                    trigger="calendar_sync",
                )
                communication_deal_mapping = (
                    await CommunicationDealMappingTriggerService(
                        org_id
                    ).trigger_for_changed_communications(
                        integration_id=int_id,
                        source_kind="calendar",
                        trigger_source=communication_trigger_source,
                        changed_record_ids=sync_result.get("changed_event_ids", []),
                        sync_started_at=started_at_dt,
                    )
                ).to_dict()

            logger.info(
                "Manual calendar sync complete",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "events_synced": records_synced,
                    "records_created": sync_result.get("created", 0),
                    "records_updated": sync_result.get("updated", 0),
                    "skipped": sync_result.get("skipped", 0),
                    "cancelled_existing": sync_result.get("cancelled_existing", 0),
                    "cancelled_no_record": sync_result.get("cancelled_no_record", 0),
                    "deleted_processed": sync_result.get("deleted_processed", 0),
                    "deleted_no_state": sync_result.get("deleted_no_state", 0),
                    "records_failed": sync_result.get("records_failed", 0),
                },
            )

            return {
                "status": sync_status,
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "integration_type": fetch_data.integration_type_value,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "events_synced": records_synced,
                "sync_breakdown": sync_result,
                "communication_deal_mapping": communication_deal_mapping,
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
                "Manual calendar sync succeeded",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "events_synced": result.get("events_synced", 0),
                },
            )
            _record_receipt(SyncStatus.SUCCESS, completed=True)
        else:
            logger.warning(
                "Manual calendar sync failed",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": result.get("error"),
                },
            )
            if trigger_source == "webhook":
                if is_terminal_webhook_result(result.get("error")):
                    _record_receipt(
                        SyncStatus.FAILED,
                        metadata={"failure_stage": "worker_result", "terminal": True},
                        error=str(result.get("error") or "Webhook calendar sync failed"),
                        completed=True,
                    )
                    return result
                raise RuntimeError(str(result.get("error") or "Webhook calendar sync failed"))

        return result

    except SoftTimeLimitExceeded:
        logger.error(
            "Manual calendar sync timed out",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "soft_limit_seconds": _L["soft"],
            },
        )
        try:
            run_async(
                record_sync_failure(
                    organisation_id=org_id,
                    integration_id=int_id,
                    error=TimeoutError(f"Soft time limit exceeded ({_L['soft']}s)"),
                    capability=current_capability[0],
                )
            )
        except Exception:
            logger.warning(
                "Failed to record timeout failure",
                extra={"integration_id": integration_id},
            )
        _record_receipt(
            SyncStatus.FAILED,
            metadata={"failure_stage": "worker_timeout", "terminal": False},
            error=f"Task exceeded soft time limit of {_L['soft']}s",
            completed=False,
        )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_L['soft']}s",
        }
    except Exception as e:
        logger.error(
            "Manual calendar sync exception",
            exc_info=True,
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "error": str(e),
            },
        )

        failure_type = classify_sync_error(e)
        terminal = is_action_required_failure(failure_type)

        # Provider/manual sync failures are retry-safe: the next attempt reuses
        # persisted cursors and writes idempotently through the sync services.
        # Permission and authentication failures require user action, so retrying
        # them only delays the reconnect signal and cannot heal the integration.
        if not terminal and self.request.retries < self.max_retries:
            _record_receipt(
                SyncStatus.IN_PROGRESS,
                metadata={"retry_scheduled": self.request.retries + 1},
                error=str(e),
            )
            logger.info(
                "Retrying manual calendar sync",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
            )
            raise self.retry(exc=e) from e

        try:
            run_async(
                record_sync_failure(
                    organisation_id=org_id,
                    integration_id=int_id,
                    error=e,
                    capability=current_capability[0],
                )
            )
        except Exception:
            logger.warning(
                "Failed to record terminal calendar sync failure",
                extra={"integration_id": integration_id},
            )

        _record_receipt(
            SyncStatus.FAILED,
            metadata={
                "failure_stage": "dead_letter",
                "failure_type": failure_type.value,
                "terminal": terminal,
            },
            error=str(e),
            completed=terminal,
        )
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
