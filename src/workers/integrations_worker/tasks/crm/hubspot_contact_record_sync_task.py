"""Tier 3: HubSpot contacts record sync for one integration.

Triggered by:
  - Post-connect chain (link 5, between companies and deals).
  - Future manual re-run endpoint (`--contacts-only` in the dispatch script).

Chain-correctness invariants identical to earlier record phases:
  - Re-raises on SoftTimeLimitExceeded so the chain aborts via link_error.
  - Does NOT write `connection_metadata.initial_sync_status` — that's owned
    by the chain's terminal finalize / failure tasks.
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
from src.service.integrations.crm.records.contacts.contact_sync_service import (
    ContactSyncConfigurationError,
    HubSpotContactRecordSyncService,
)
from src.service.integrations.sync_health import record_sync_failure
from src.service.integrations.sync_history_manager import (
    SyncHistoryManager,
    datetime_now_naive,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_contacts"
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
def sync_hubspot_contacts_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        logger.info(
            "HubSpot contacts record sync starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        sync_id: UUID | None = None
        async for session in acquire_org_async_session(org_id):
            sync_id = await SyncHistoryManager.start_fresh(session, int_id)

        try:
            result = await HubSpotContactRecordSyncService().run(
                organisation_id=org_id,
                integration_id=int_id,
            )
        except ContactSyncConfigurationError as exc:
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
                records_processed=(result.contacts_created + result.contacts_updated),
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
        run_async(
            record_sync_failure(
                organisation_id=org_id,
                integration_id=int_id,
                error=TimeoutError("HubSpot contacts record sync timed out"),
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
    if sync_id is None:
        return
    async for session in acquire_org_async_session(org_id):
        await SyncHistoryManager.complete_fresh(
            session,
            sync_id,
            status=SyncStatus.FAILED,
            records_processed=0,
            metadata={
                "entity": "record",
                "object_type": "contact",
                "error_class": error_class,
            },
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
    """Persist outcome under `connection_metadata.record_sync.contacts`.

    This task NEVER touches `initial_sync_status` — that field is owned by
    the chain's terminal finalize task and the link_error terminator.
    """
    stmt = select(KnowledgeIntegration).where(
        col(KnowledgeIntegration.integration_id) == integration_id,
    )
    integration = (await session.execute(stmt)).scalar_one_or_none()
    if integration is None:
        return
    metadata = dict(integration.connection_metadata or {})
    record_sync = dict(metadata.get("record_sync") or {})
    contacts_entry = {
        "status": status,
        "last_synced_at": datetime.now(UTC).isoformat(),
        "fetched_contacts": result_metadata.get("fetched_contacts"),
        "contacts_created": result_metadata.get("contacts_created"),
        "contacts_updated": result_metadata.get("contacts_updated"),
        "unresolved_company_link": result_metadata.get("unresolved_company_link"),
        "unresolved_owner_link": result_metadata.get("unresolved_owner_link"),
        "skipped_malformed": result_metadata.get("skipped_malformed"),
        "deal_backfill_rows_updated": result_metadata.get("deal_backfill_rows_updated"),
        "deal_backfill_per_op": result_metadata.get("deal_backfill_per_op"),
        "error_class": error_class,
        "error_message": error_message,
    }
    record_sync["contacts"] = contacts_entry
    metadata["record_sync"] = record_sync
    integration.connection_metadata = metadata
    integration.last_synced_at = datetime_now_naive()
    session.add(integration)
    await session.commit()
