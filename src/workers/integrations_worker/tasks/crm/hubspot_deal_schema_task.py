"""Tier 3: HubSpot deal schema sync for a single integration.

Triggered by:
  - InitialSyncService post-connect dispatch
  - Tier 1 fan-out task on the 14-day schedule
  - Future "Refresh schema" manual endpoint

Session ownership:
  - Task owns sync-history and connection-metadata sessions (Session 1: start,
    Session 3: complete + metadata).
  - HubSpotDealSchemaSyncService owns its own extract + persist sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.schema_sync_service import (
    HubSpotDealSchemaSyncService,
    SchemaSyncConfigurationError,
)
from src.service.integrations.sync_health import record_sync_failure
from src.service.integrations.sync_history_manager import (
    SyncHistoryManager,
    datetime_now_naive,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_deal_schema"
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
def sync_hubspot_deal_schema_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        logger.info(
            "HubSpot deal schema sync starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        sync_id: UUID | None = None
        async for session in acquire_org_async_session(org_id):
            # Keep sync history creation in its own short-lived session to avoid
            # coupling lifecycle with provider API calls.
            sync_id = await SyncHistoryManager.start_fresh(session, int_id)

        try:
            result = await HubSpotDealSchemaSyncService().run(
                organisation_id=org_id,
                integration_id=int_id,
            )
        except SchemaSyncConfigurationError as exc:
            await _mark_history_failed(
                org_id,
                sync_id,
                error_class="configuration",
                error_message=str(exc),
            )
            raise

        async for session in acquire_org_async_session(org_id):
            await SyncHistoryManager.complete_fresh(
                session,
                sync_id,  # type: ignore[arg-type]
                status=SyncStatus.SUCCESS,
                records_processed=result.custom_properties_persisted,
                metadata={
                    "entity": "schema",
                    **result.to_metadata(),
                },
            )
            await _update_integration_metadata(
                session=session,
                integration_id=int_id,
                status="success",
                result_metadata=result.to_metadata(),
                error_class=None,
                error_message=None,
            )

        completed_at = datetime.now(UTC).isoformat()
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": completed_at,
            **result.to_metadata(),
        }

    try:
        return run_async(_run())
    except SoftTimeLimitExceeded:
        # Record the failure, then re-raise so the Celery chain aborts via
        # link_error. Returning a dict would succeed the task in Celery's eyes
        # and let downstream chain links run against a half-finished state.
        run_async(
            record_sync_failure(
                organisation_id=org_id,
                integration_id=int_id,
                error=TimeoutError("HubSpot deal schema sync timed out"),
            )
        )
        raise


async def _mark_history_failed(
    org_id: UUID,
    sync_id: UUID | None,
    *,
    error_class: str,
    error_message: str,
) -> None:
    """Mark the active sync history row as failed with machine-readable context."""

    if sync_id is None:
        return
    async for session in acquire_org_async_session(org_id):
        await SyncHistoryManager.complete_fresh(
            session,
            sync_id,
            status=SyncStatus.FAILED,
            records_processed=0,
            metadata={"entity": "schema", "error_class": error_class},
            error_details={"message": error_message, "class": error_class},
        )


async def _update_integration_metadata(
    *,
    session,
    integration_id: UUID,
    status: str,
    result_metadata: dict[str, Any],
    error_class: str | None,
    error_message: str | None,
) -> None:
    """Persist schema sync outcome under `connection_metadata.schema_sync.deals`."""

    stmt = select(KnowledgeIntegration).where(
        col(KnowledgeIntegration.integration_id) == integration_id,
    )
    integration = (await session.execute(stmt)).scalar_one_or_none()
    if integration is None:
        return
    metadata = dict(integration.connection_metadata or {})
    schema_sync = dict(metadata.get("schema_sync") or {})
    deals_entry = {
        "status": status,
        "last_synced_at": datetime.now(UTC).isoformat(),
        "fetched_properties": result_metadata.get("fetched_properties"),
        "canonical_properties": result_metadata.get("canonical_properties"),
        "custom_properties_persisted": result_metadata.get("custom_properties_persisted"),
        "created": result_metadata.get("created"),
        "updated": result_metadata.get("updated"),
        "reactivated": result_metadata.get("reactivated"),
        "deactivated": result_metadata.get("deactivated"),
        "skipped": result_metadata.get("skipped"),
        "error_class": error_class,
        "error_message": error_message,
    }
    schema_sync["deals"] = deals_entry
    metadata["schema_sync"] = schema_sync
    # initial_sync_status is owned exclusively by the chain's terminal finalize
    # task (mark_hubspot_initial_sync_succeeded) and the link_error terminator
    # (mark_hubspot_initial_sync_failed). Per-phase tasks must not write it —
    # otherwise schema success would mark the whole initial sync done before
    # downstream phases run.
    integration.connection_metadata = metadata
    integration.last_synced_at = datetime_now_naive()
    session.add(integration)
    await session.commit()
