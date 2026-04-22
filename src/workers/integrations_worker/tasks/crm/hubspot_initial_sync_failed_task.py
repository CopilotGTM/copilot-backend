"""Tier 3 chain link_error terminator: mark a HubSpot initial sync as failed.

Attached as `link_error` on the chain returned by build_hubspot_initial_sync_chain.
Fires whenever any chain link raises (which is also our timeout path — phase
tasks re-raise on SoftTimeLimitExceeded).

Sets `connection_metadata.initial_sync_status = "failed"` and stamps which
chain link's task name failed (best-effort; Celery passes the failed task's
UUID, name resolution falls back to "unknown" if the result backend doesn't
have it).

Does NOT re-raise — this task is the terminator of the chain's error branch.
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
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import get_integrations_celery_client
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.mark_hubspot_initial_sync_failed"
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
def mark_hubspot_initial_sync_failed_task(
    self: Task,
    *args: Any,
    organisation_id: str,
    integration_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Record initial-sync failure.

    Celery passes the failed task's UUID as the first positional arg when
    invoked as a chain `link_error`. We accept *args so manual invocations
    (which only pass kwargs) also work.
    """
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
                col(KnowledgeIntegration.integration_id) == int_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                logger.warning(
                    "Cannot mark initial sync failed — integration not found",
                    extra={"integration_id": integration_id},
                )
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

        logger.warning(
            "HubSpot initial sync marked failed",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "failed_phase": failed_phase,
            },
        )
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "failed_phase": failed_phase,
        }

    return run_async(_run())


def _resolve_failed_phase(task_uuid: str) -> str | None:
    """Best-effort lookup of the failed task's name for observability.

    Returns None if the result backend doesn't carry the name (which is fine —
    the phase will still appear in logs).
    """
    try:
        client = get_integrations_celery_client()
        async_result = client.AsyncResult(task_uuid)
        return getattr(async_result, "name", None) or getattr(async_result, "task_name", None)
    except Exception:  # pragma: no cover — observability fallback only
        logger.exception("Failed to resolve failed-phase task name")
        return None
