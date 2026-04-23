"""Tier 1: fan-out Salesforce Opportunity schema sync for all active Salesforce integrations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_salesforce_opportunity_schema_all_orgs"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_salesforce_opportunity_schema_all_orgs_task(self: Task) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()

    async def _run() -> dict[str, Any]:
        dispatched = 0
        skipped_inactive = 0

        rows: list[KnowledgeIntegration] = []
        async for session in acquire_master_async_session():
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_type) == IntegrationType.SALESFORCE,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            rows = list((await session.execute(stmt)).scalars().all())

        for row in rows:
            if not row.is_active:
                skipped_inactive += 1
                continue
            send_integrations_task(
                task_name="integrations.sync_salesforce_opportunity_schema",
                kwargs={
                    "organisation_id": str(row.organisation_id),
                    "integration_id": str(row.integration_id),
                },
            )
            dispatched += 1

        return {
            "status": "success",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "dispatched": dispatched,
            "skipped_inactive": skipped_inactive,
        }

    return run_async(_run())
