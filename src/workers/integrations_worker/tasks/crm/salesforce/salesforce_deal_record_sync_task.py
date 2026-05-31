"""Tier 3: Salesforce deals fact sync for one integration."""

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
from src.service.integrations.crm.records.salesforce.deals.deal_sync_service import (
    SalesforceDealRecordSyncService,
    SalesforceDealSyncConfigurationError,
)
from src.service.integrations.sync_health import record_sync_failure
from src.service.integrations.sync_history_manager import (
    SyncHistoryManager,
    datetime_now_naive,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task_async
from src.workers.integrations_worker.config import TASK_LIMITS
from src.workers.integrations_worker.tasks.crm.failure_handling import (
    record_crm_task_failure,
)

_TASK_NAME = "integrations.sync_salesforce_deals"
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
def sync_salesforce_deals_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
    start_cdc_after_success: bool = False,
    cdc_channel: str = "/data/OpportunityChangeEvent",
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        sync_id: UUID | None = None
        async for session in acquire_org_async_session(org_id):
            sync_id = await SyncHistoryManager.start_fresh(session, int_id)

        try:
            result = await SalesforceDealRecordSyncService().run(
                organisation_id=org_id,
                integration_id=int_id,
            )
        except SalesforceDealSyncConfigurationError as exc:
            await record_crm_task_failure(
                organisation_id=org_id,
                integration_id=int_id,
                sync_id=sync_id,
                error_class="configuration",
                error_message=str(exc),
                error=exc,
                mark_history_failed=_mark_history_failed,
                update_integration_metadata=_update_integration_metadata,
            )
            raise
        except Exception as exc:
            await record_crm_task_failure(
                organisation_id=org_id,
                integration_id=int_id,
                sync_id=sync_id,
                error_class=type(exc).__name__,
                error_message=str(exc),
                error=exc,
                mark_history_failed=_mark_history_failed,
                update_integration_metadata=_update_integration_metadata,
            )
            raise

        async for session in acquire_org_async_session(org_id):
            await SyncHistoryManager.complete_fresh(
                session,
                sync_id,  # type: ignore[arg-type]
                status=SyncStatus.SUCCESS,
                records_processed=(result.deals_created + result.deals_updated),
                metadata=result.to_metadata(),
            )
            await _update_integration_metadata(
                session=session,
                organisation_id=org_id,
                integration_id=int_id,
                status="success",
                result_metadata=result.to_metadata(),
                error_class=None,
                error_message=None,
            )

        cdc_task_id = None
        if start_cdc_after_success:
            cdc_task_id = await send_integrations_task_async(
                task_name="integrations.run_salesforce_cdc_subscriber",
                kwargs={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "channel": cdc_channel,
                },
                queue="integrations_streaming",
                routing_key="integrations_streaming",
            )

        completed_at = datetime.now(UTC).isoformat()
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "salesforce_cdc_task_id": cdc_task_id,
            **result.to_metadata(),
        }

    try:
        return run_async(_run())
    except SoftTimeLimitExceeded:
        run_async(
            record_sync_failure(
                organisation_id=org_id,
                integration_id=int_id,
                error=TimeoutError("Salesforce deals record sync timed out"),
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
            metadata={"entity": "record", "object_type": "deal", "error_class": error_class},
            error_details={"message": error_message, "class": error_class},
        )


async def _update_integration_metadata(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    status: str,
    result_metadata: dict[str, Any],
    error_class: str | None,
    error_message: str | None,
) -> None:
    stmt = select(KnowledgeIntegration).where(
        col(KnowledgeIntegration.organisation_id) == organisation_id,
        col(KnowledgeIntegration.integration_id) == integration_id,
    )
    integration = (await session.execute(stmt)).scalar_one_or_none()
    if integration is None:
        return
    metadata = dict(integration.connection_metadata or {})
    record_sync = dict(metadata.get("record_sync") or {})
    deals_entry = {
        "status": status,
        "last_synced_at": datetime.now(UTC).isoformat(),
        "fetched_opportunities": result_metadata.get("fetched_opportunities"),
        "property_list_size": result_metadata.get("property_list_size"),
        "fetched_stage_history_rows": result_metadata.get("fetched_stage_history_rows"),
        "fetched_participant_rows": result_metadata.get("fetched_participant_rows"),
        "deals_created": result_metadata.get("deals_created"),
        "deals_updated": result_metadata.get("deals_updated"),
        "unresolved_pipeline_link": result_metadata.get("unresolved_pipeline_link"),
        "unresolved_stage_link": result_metadata.get("unresolved_stage_link"),
        "unresolved_owner_link": result_metadata.get("unresolved_owner_link"),
        "unresolved_company_link": result_metadata.get("unresolved_company_link"),
        "unresolved_contact_link": result_metadata.get("unresolved_contact_link"),
        "unresolved_created_by_link": result_metadata.get("unresolved_created_by_link"),
        "unresolved_updated_by_link": result_metadata.get("unresolved_updated_by_link"),
        "skipped_malformed_opportunities": result_metadata.get("skipped_malformed_opportunities"),
        "skipped_malformed_stage_history": result_metadata.get("skipped_malformed_stage_history"),
        "skipped_malformed_participants": result_metadata.get("skipped_malformed_participants"),
        "stage_history_created": result_metadata.get("stage_history_created"),
        "stage_history_updated": result_metadata.get("stage_history_updated"),
        "stage_history_unresolved_stage": result_metadata.get("stage_history_unresolved_stage"),
        "stage_history_unresolved_from_stage": result_metadata.get(
            "stage_history_unresolved_from_stage"
        ),
        "participants_created": result_metadata.get("participants_created"),
        "participants_updated": result_metadata.get("participants_updated"),
        "participants_unresolved_related": result_metadata.get("participants_unresolved_related"),
        "associations_created": result_metadata.get("associations_created"),
        "associations_updated": result_metadata.get("associations_updated"),
        "associations_unresolved_to_local": result_metadata.get("associations_unresolved_to_local"),
        "deal_backfill_rows_updated": result_metadata.get("deal_backfill_rows_updated"),
        "deal_backfill_per_op": result_metadata.get("deal_backfill_per_op"),
        "error_class": error_class,
        "error_message": error_message,
    }
    record_sync["deals"] = deals_entry
    metadata["record_sync"] = record_sync
    integration.connection_metadata = metadata
    integration.last_synced_at = datetime_now_naive()
    session.add(integration)
    await session.commit()
