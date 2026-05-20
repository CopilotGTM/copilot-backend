"""Tier 3: HubSpot core incremental repair for one org-scoped integration.

Called by the Tier 1 HubSpot fan-out task after initial sync has established a
safe starting cursor. This worker owns queue-level retry/timeout handling only;
the service owns tenant validation, provider polling, full-object refresh, and
cursor advancement after successful writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.core_incremental_sync_service import (
    HubSpotCoreIncrementalSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.registry import (
    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_hubspot_core_incremental"
_L = TASK_LIMITS[_TASK_NAME]
# Keep the object-family list centralized with the service registry. Adding a
# new HubSpot object to incremental sync should update the registry and the
# service refresher, not hand-edit individual worker payloads.
_OBJECT_KEYS = HUBSPOT_INCREMENTAL_OBJECT_KEYS


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_hubspot_core_incremental_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Refresh changed HubSpot core CRM objects for one active integration.

    The task is safe to retry because each object refresh writes a full current
    HubSpot snapshot and each writer skips stale provider timestamps. Cursor
    metadata is advanced only after the service finishes the requested object
    families, so a worker crash replays the overlap window on the next attempt.
    """

    # Celery receives ids as JSON strings. Convert them before the async service
    # boundary so malformed payloads fail quickly and never reach provider calls.
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        # The service re-checks the org/integration boundary before reading
        # tokens or writing cursors. The queue payload is not trusted as proof
        # that the integration belongs to this organisation.
        logger.info(
            "HubSpot core incremental sync starting",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )
        # This call performs provider I/O and short-lived DB writes internally.
        # The task does not open a DB session, so slow HubSpot pages or token
        # refreshes cannot pin a worker-side tenant connection here.
        result = await HubSpotCoreIncrementalSyncService().run(
            organisation_id=org_id,
            integration_id=int_id,
            object_keys=_OBJECT_KEYS,
        )
        completed_at = datetime.now(UTC).isoformat()
        # Return aggregate counters for Celery observability. Per-object cursor
        # details remain in `connection_metadata.record_sync` so the task result
        # stays small and safe to store in the broker/backend.
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "object_keys": list(_OBJECT_KEYS),
            "seen_count": result.seen_count,
            "written_count": result.written_count,
            "skipped_stale_count": result.skipped_stale_count,
        }

    try:
        # Celery tasks are sync callables, while the CRM sync service is async.
        # `run_async` owns the event-loop bridge used consistently by workers.
        return run_async(_run())
    except SoftTimeLimitExceeded:
        # Soft timeouts are not retried here. Let Celery enforce its timeout
        # policy so a long provider run cannot continue past the worker limit.
        logger.exception(
            "HubSpot core incremental sync timed out",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )
        raise
    except Exception as exc:
        if self.request.retries < self.max_retries:
            # Transient provider/auth/database failures are retried by replaying
            # the same integration. Cursor advancement happens after service
            # completion, so retries re-read the previous overlap instead of
            # skipping missed HubSpot changes.
            logger.warning(
                "HubSpot core incremental sync failed; retrying",
                exc_info=True,
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "attempt": self.request.retries + 1,
                },
            )
            raise self.retry(exc=exc, countdown=_L.get("retry_delay", 60)) from exc
        # The final failure is logged with tenant and integration ids so ops can
        # isolate one broken connection without confusing it with global fan-out.
        logger.exception(
            "HubSpot core incremental sync failed after retries",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )
        raise
