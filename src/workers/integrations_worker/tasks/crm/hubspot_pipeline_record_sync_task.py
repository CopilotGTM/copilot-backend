"""Tier 3: HubSpot deal pipelines + stages record sync for one integration.

Triggered by:
  - The HubSpot post-connect Celery chain (between schema sync and the next phase).
  - Future "Refresh pipelines" manual endpoint.

Session ownership:
  - Task owns sync-history sessions (start + complete) and the
    connection_metadata.record_sync.pipelines update session.
  - HubSpotPipelineRecordSyncService owns its own extract + persist sessions.

Chain-correctness invariants:
  - On SoftTimeLimitExceeded, this task RE-RAISES so the chain aborts via
    link_error. It must never return a dict on timeout.
  - This task does NOT write `connection_metadata.initial_sync_status`. That
    field is owned by the chain's terminal finalize task.
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
from src.service.integrations.crm.records.pipelines.pipeline_sync_service import (
    HubSpotPipelineRecordSyncService,
    PipelineSyncConfigurationError,
)
from src.service.integrations.sync_health import record_sync_failure
from src.service.integrations.sync_history_manager import (
    SyncHistoryManager,
    datetime_now_naive,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_pipelines"
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
def sync_hubspot_pipelines_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        logger.info(
            "HubSpot pipelines record sync starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        sync_id: UUID | None = None
        async for session in acquire_org_async_session(org_id):
            sync_id = await SyncHistoryManager.start_fresh(session, int_id)

        try:
            result = await HubSpotPipelineRecordSyncService().run(
                organisation_id=org_id,
                integration_id=int_id,
            )
        except PipelineSyncConfigurationError as exc:
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
                records_processed=(
                    result.pipelines_created
                    + result.pipelines_updated
                    + result.stages_created
                    + result.stages_updated
                ),
                metadata=result.to_metadata(),
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
        # Record the failure, then re-raise so the chain aborts via link_error.
        # Returning would succeed the task in Celery's eyes and let downstream
        # links run against an incomplete pipelines snapshot.
        run_async(
            record_sync_failure(
                organisation_id=org_id,
                integration_id=int_id,
                error=TimeoutError("HubSpot pipelines record sync timed out"),
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
            metadata={"entity": "record", "object_type": "pipeline", "error_class": error_class},
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
    """Persist outcome under `connection_metadata.record_sync.pipelines`.

    This task NEVER touches `initial_sync_status` — that field is owned by the
    chain's terminal finalize task (mark_hubspot_initial_sync_succeeded) and
    the link_error terminator (mark_hubspot_initial_sync_failed).
    """
    stmt = select(KnowledgeIntegration).where(
        col(KnowledgeIntegration.integration_id) == integration_id,
    )
    integration = (await session.execute(stmt)).scalar_one_or_none()
    if integration is None:
        return
    metadata = dict(integration.connection_metadata or {})
    record_sync = dict(metadata.get("record_sync") or {})
    pipelines_entry = {
        "status": status,
        "last_synced_at": datetime.now(UTC).isoformat(),
        "fetched_pipelines": result_metadata.get("fetched_pipelines"),
        "fetched_stages": result_metadata.get("fetched_stages"),
        "pipelines_created": result_metadata.get("pipelines_created"),
        "pipelines_updated": result_metadata.get("pipelines_updated"),
        "pipelines_reactivated": result_metadata.get("pipelines_reactivated"),
        "pipelines_deactivated": result_metadata.get("pipelines_deactivated"),
        "stages_created": result_metadata.get("stages_created"),
        "stages_updated": result_metadata.get("stages_updated"),
        "stages_reactivated": result_metadata.get("stages_reactivated"),
        "stages_deactivated": result_metadata.get("stages_deactivated"),
        "skipped_pipelines": result_metadata.get("skipped_pipelines"),
        "skipped_stages": result_metadata.get("skipped_stages"),
        "error_class": error_class,
        "error_message": error_message,
    }
    record_sync["pipelines"] = pipelines_entry
    metadata["record_sync"] = record_sync
    integration.connection_metadata = metadata
    integration.last_synced_at = datetime_now_naive()
    session.add(integration)
    await session.commit()
