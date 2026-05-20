"""Tier 3: slow HubSpot delete reconciliation for one integration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.deletion_reconciliation_service import (
    HubSpotDeletionReconciliationService,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.reconcile_hubspot_deletions"
_L = TASK_LIMITS[_TASK_NAME]
_OBJECT_KEYS = ("deals", "companies", "contacts", "tasks", "notes")


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 120),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def reconcile_hubspot_deletions_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Verify active local HubSpot ids and tombstone confirmed missing objects."""

    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        result = await HubSpotDeletionReconciliationService().run(
            organisation_id=org_id,
            integration_id=int_id,
            object_keys=_OBJECT_KEYS,
        )
        return {
            "status": "success",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "object_keys": list(_OBJECT_KEYS),
            "checked_count": result.checked_count,
            "missing_count": result.missing_count,
            "soft_deleted_count": result.soft_deleted_count,
        }

    try:
        return run_async(_run())
    except SoftTimeLimitExceeded:
        logger.exception(
            "HubSpot delete reconciliation timed out",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )
        raise
    except Exception as exc:
        if self.request.retries < self.max_retries:
            logger.warning(
                "HubSpot delete reconciliation failed; retrying",
                exc_info=True,
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "attempt": self.request.retries + 1,
                },
            )
            raise self.retry(exc=exc, countdown=_L.get("retry_delay", 120)) from exc
        logger.exception(
            "HubSpot delete reconciliation failed after retries",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )
        raise
