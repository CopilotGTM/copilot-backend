"""Tier 1: fan-out HubSpot deal schema sync for all active HubSpot integrations.

Triggered by the Kubernetes CronJob that calls
`python -m src.workers.integrations_worker.cron_dispatch --job sync-hubspot-deal-schema-all-orgs`.

Reads approved orgs from the master schema, then opens a per-org session for
each to enumerate HubSpot `knowledge_integrations` rows before dispatching the
Tier 3 task. `knowledge_integrations` is an org-scoped table, so it is only
reachable with an org-scoped `search_path` — the master session cannot see it.
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
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_deal_schema_all_orgs"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_hubspot_deal_schema_all_orgs_task(self: Task) -> dict[str, Any]:
    """Tier-1 fan-out: dispatch HubSpot deal schema sync for every active integration."""

    started_at = datetime.now(UTC).isoformat()

    async def _run() -> dict[str, Any]:
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        dispatched = 0
        skipped_inactive = 0
        orgs_with_integrations = 0
        orgs_failed = 0

        for org_id in org_ids:
            try:
                integrations: list[tuple[UUID, UUID, bool]] = []
                async for session in acquire_org_async_session(org_id):
                    stmt = select(KnowledgeIntegration).where(
                        col(KnowledgeIntegration.integration_type) == IntegrationType.HUBSPOT,
                        col(KnowledgeIntegration.is_active).is_(True),
                    )
                    rows = (await session.execute(stmt)).scalars().all()
                    integrations = [
                        (row.organisation_id, row.integration_id, row.is_active) for row in rows
                    ]
            except Exception as exc:
                orgs_failed += 1
                logger.error(
                    "HubSpot fan-out: failed to load integrations for org",
                    exc_info=True,
                    extra={"organisation_id": str(org_id), "error": str(exc)},
                )
                continue

            if not integrations:
                continue
            orgs_with_integrations += 1

            for org_uuid, integration_id, is_active in integrations:
                if not is_active:
                    # Defensive: the query already filters is_active = True, but guard
                    # against stale snapshots before dispatching Tier-3 work.
                    skipped_inactive += 1
                    continue
                send_integrations_task(
                    task_name="integrations.sync_hubspot_deal_schema",
                    kwargs={
                        "organisation_id": str(org_uuid),
                        "integration_id": str(integration_id),
                    },
                )
                dispatched += 1

        logger.info(
            "HubSpot deal schema fan-out complete",
            extra={
                "orgs_scanned": len(org_ids),
                "orgs_with_integrations": orgs_with_integrations,
                "orgs_failed": orgs_failed,
                "dispatched": dispatched,
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
            "skipped_inactive": skipped_inactive,
        }

    return run_async(_run())
