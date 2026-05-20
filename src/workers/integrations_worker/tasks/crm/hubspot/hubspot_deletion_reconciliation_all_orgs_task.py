"""Tier 1: fan-out slow HubSpot delete reconciliation for ready integrations."""

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
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.reconcile_hubspot_deletions_all_orgs"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def reconcile_hubspot_deletions_all_orgs_task(self: Task) -> dict[str, Any]:
    """Dispatch one HubSpot deletion-reconciliation task for each ready integration."""
    started_at = datetime.now(UTC).isoformat()

    async def _run() -> dict[str, Any]:
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            org_stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(org_stmt)
            org_ids = [row[0] for row in result.all()]

        dispatched = 0
        skipped_not_ready = 0
        orgs_with_integrations = 0
        orgs_failed = 0

        for org_id in org_ids:
            try:
                integrations: list[KnowledgeIntegration] = []
                async for session in acquire_org_async_session(org_id):
                    integration_stmt = select(KnowledgeIntegration).where(
                        col(KnowledgeIntegration.integration_type) == IntegrationType.HUBSPOT,
                        col(KnowledgeIntegration.is_active).is_(True),
                    )
                    integrations = list((await session.execute(integration_stmt)).scalars().all())
            except Exception as exc:
                orgs_failed += 1
                logger.error(
                    "HubSpot delete reconciliation fan-out failed to load integrations for org",
                    exc_info=True,
                    extra={"organisation_id": str(org_id), "error": str(exc)},
                )
                continue

            if not integrations:
                continue
            orgs_with_integrations += 1

            for integration in integrations:
                if not _is_reconciliation_ready(integration.connection_metadata):
                    skipped_not_ready += 1
                    continue
                send_integrations_task(
                    task_name="integrations.reconcile_hubspot_deletions",
                    kwargs={
                        "organisation_id": str(integration.organisation_id),
                        "integration_id": str(integration.integration_id),
                    },
                )
                dispatched += 1

        return {
            "status": "success",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "orgs_scanned": len(org_ids),
            "orgs_with_integrations": orgs_with_integrations,
            "orgs_failed": orgs_failed,
            "dispatched": dispatched,
            "skipped_not_ready": skipped_not_ready,
        }

    return run_async(_run())


def _is_reconciliation_ready(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    if metadata.get("initial_sync_status") == "success":
        return True
    record_sync = metadata.get("record_sync")
    if not isinstance(record_sync, dict):
        return False
    return all(
        isinstance(record_sync.get(object_key), dict)
        and bool(record_sync[object_key].get("last_synced_at"))
        for object_key in ("deals", "companies", "contacts", "tasks", "notes")
    )
