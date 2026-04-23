"""Tier 3 chain link_error terminator: mark a Salesforce initial sync as failed."""

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
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import get_integrations_celery_client
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.mark_salesforce_initial_sync_failed"
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
def mark_salesforce_initial_sync_failed_task(
    self: Task,
    *args: Any,
    organisation_id: str,
    integration_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    failed_task_uuid: str | None = None
    if args:
        first = args[0]
        if isinstance(first, str):
            failed_task_uuid = first

    failed_phase = _resolve_failed_phase(failed_task_uuid) if failed_task_uuid else None

    async def _run() -> dict[str, Any]:
        async for session in acquire_org_async_session(org_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == org_id,
                col(KnowledgeIntegration.integration_id) == int_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                return {
                    "status": "noop",
                    "reason": "integration_not_found",
                    "integration_id": integration_id,
                }

            metadata = dict(integration.connection_metadata or {})
            metadata["initial_sync_status"] = "failed"
            metadata["initial_sync_failed_at"] = datetime.now(UTC).isoformat()
            if failed_phase:
                metadata["initial_sync_failed_phase"] = failed_phase
            integration.connection_metadata = metadata
            session.add(integration)
            await session.commit()

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "failed_phase": failed_phase,
        }

    return run_async(_run())


def _resolve_failed_phase(task_uuid: str) -> str | None:
    try:
        client = get_integrations_celery_client()
        async_result = client.AsyncResult(task_uuid)
        return getattr(async_result, "name", None) or getattr(async_result, "task_name", None)
    except Exception:  # pragma: no cover
        logger.exception("Failed to resolve failed-phase task name")
        return None
