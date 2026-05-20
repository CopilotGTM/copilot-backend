"""Tier 1: scheduled HubSpot incremental fan-out across approved tenants.

Called by the integrations worker CronJob every few minutes. This module owns
tenant discovery and child-task dispatch only; provider polling, full-object
refresh, writer idempotency, and cursor advancement stay isolated in the Tier 3
per-integration task.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.incremental.registry import (
    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
)
from src.service.integrations.crm.records.hubspot.incremental_cursor_metadata import (
    is_hubspot_incremental_ready,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_core_incremental_all_orgs"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_hubspot_core_incremental_all_orgs_task(self: Task) -> dict[str, Any]:
    """Dispatch HubSpot core incremental sync for every post-initial-sync integration.

    The task is retry-safe at the fan-out layer: duplicate dispatch for the same
    integration only causes the child task to replay its timestamp lookback and
    rely on stale-snapshot checks in the writers. The Kubernetes CronJob should
    still keep `concurrencyPolicy: Forbid` so overlapping scans do not create
    avoidable queue pressure.
    """

    started_at = datetime.now(UTC).isoformat()

    async def _run() -> dict[str, Any]:
        # --- Step 1: discover tenant schemas that are allowed to run sync ---
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            # Master data is used only to enumerate approved organisations. CRM
            # integration rows are tenant data, so each org is opened through
            # its own schema below instead of reading integrations globally.
            org_stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(org_stmt)
            org_ids = [row[0] for row in result.all()]

        # --- Step 2: scan each tenant for HubSpot integrations ready to poll ---
        dispatched = 0
        skipped_not_ready = 0
        skipped_inactive = 0
        orgs_with_integrations = 0
        orgs_failed = 0

        for org_id in org_ids:
            try:
                integrations: list[KnowledgeIntegration] = []
                async for session in acquire_org_async_session(org_id):
                    # The org session is the isolation boundary. Discovering
                    # integrations inside the tenant schema prevents a queued
                    # fan-out from ever mixing integration ids across orgs.
                    integration_stmt = select(KnowledgeIntegration).where(
                        col(KnowledgeIntegration.integration_type) == IntegrationType.HUBSPOT,
                        col(KnowledgeIntegration.is_active).is_(True),
                    )
                    integration_result = await session.execute(integration_stmt)
                    integrations = list(integration_result.scalars().all())
            except Exception as exc:
                orgs_failed += 1
                # One broken tenant must not block the global poller. Logging
                # and continuing lets the next scheduled tick retry that org
                # while healthy tenants still receive their child tasks.
                logger.error(
                    "HubSpot core incremental fan-out failed to load integrations for org",
                    exc_info=True,
                    extra={"organisation_id": str(org_id), "error": str(exc)},
                )
                continue

            if not integrations:
                continue
            orgs_with_integrations += 1

            for integration in integrations:
                if not integration.is_active:
                    skipped_inactive += 1
                    continue
                if not is_hubspot_incremental_ready(
                    integration.connection_metadata,
                    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
                ):
                    skipped_not_ready += 1
                    continue
                # Dispatch, don't run provider work here. Child tasks own
                # HubSpot API calls and cursor writes so this global task stays
                # short and avoids holding tenant sessions during provider I/O.
                send_integrations_task(
                    task_name="integrations.sync_hubspot_core_incremental",
                    kwargs={
                        "organisation_id": str(integration.organisation_id),
                        "integration_id": str(integration.integration_id),
                    },
                )
                dispatched += 1

        # --- Step 3: report fan-out health for the scheduled job run ---
        logger.info(
            "HubSpot core incremental fan-out complete",
            extra={
                "orgs_scanned": len(org_ids),
                "orgs_with_integrations": orgs_with_integrations,
                "orgs_failed": orgs_failed,
                "dispatched": dispatched,
                "skipped_not_ready": skipped_not_ready,
                "skipped_inactive": skipped_inactive,
            },
        )
        return {
            "status": "success",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "orgs_scanned": len(org_ids),
            "orgs_with_integrations": orgs_with_integrations,
            "orgs_failed": orgs_failed,
            "dispatched": dispatched,
            "skipped_not_ready": skipped_not_ready,
            "skipped_inactive": skipped_inactive,
        }

    return run_async(_run())
