"""Tier 3 chain terminal: mark a HubSpot initial sync as fully successful.

This is the LAST link in the chain returned by build_hubspot_initial_sync_chain.
It is the SOLE writer of `connection_metadata.initial_sync_status = "success"`.

Because every prior chain link re-raises on failure, this task only runs if
all upstream links succeeded — so the success write is safe by construction.
Per-phase tasks must NOT write `initial_sync_status`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.sync_history_manager import datetime_now_naive
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.mark_hubspot_initial_sync_succeeded"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 30),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def mark_hubspot_initial_sync_succeeded_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == int_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                logger.warning(
                    "Cannot finalize initial sync — integration not found",
                    extra={"integration_id": integration_id},
                )
                return {
                    "status": "noop",
                    "reason": "integration_not_found",
                    "integration_id": integration_id,
                }

            metadata = dict(integration.connection_metadata or {})
            metadata["initial_sync_status"] = "success"
            metadata["initial_sync_completed_at"] = datetime.now(UTC).isoformat()
            metadata.pop("initial_sync_failed_phase", None)
            metadata.pop("initial_sync_placeholder", None)
            integration.connection_metadata = metadata
            integration.last_synced_at = datetime_now_naive()
            session.add(integration)
            await session.commit()

        logger.info(
            "HubSpot initial sync marked successful",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
            },
        )
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
        }

    return run_async(_run())
