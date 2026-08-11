"""Celery finalizers for the serial Salesforce aggregate CDC repair chain."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from celery import Task

from src.infra.database.sessions import acquire_org_async_session
from src.infra.observability.metrics import record_salesforce_cdc_repair
from src.infra.utils.async_runner import run_async
from src.service.integrations.webhooks.salesforce.metadata import AGGREGATE_CDC_CHANNEL
from src.service.integrations.webhooks.salesforce.subscription_state import (
    SalesforceCdcSubscriptionStateService,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

_SUCCEEDED_TASK = "integrations.mark_salesforce_cdc_repair_succeeded"
_FAILED_TASK = "integrations.mark_salesforce_cdc_repair_failed"
logger = logging.getLogger(__name__)


@app.task(
    name=_SUCCEEDED_TASK,
    bind=True,
    max_retries=TASK_LIMITS[_SUCCEEDED_TASK]["retries"],
    acks_late=True,
    soft_time_limit=TASK_LIMITS[_SUCCEEDED_TASK]["soft"],
    time_limit=TASK_LIMITS[_SUCCEEDED_TASK]["hard"],
)
def mark_salesforce_cdc_repair_succeeded_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Return the repaired connector to pending with an EARLIEST replay preset."""
    _ = self
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _run() -> dict[str, Any]:
        service = SalesforceCdcSubscriptionStateService()
        async for session in acquire_org_async_session(org_id):
            updated = await service.mark_repair_succeeded(
                session=session,
                organisation_id=org_id,
                integration_id=int_id,
                channel=AGGREGATE_CDC_CHANNEL,
            )
            if updated:
                record_salesforce_cdc_repair("completed", reason="replay_expired")
            return {"status": "pending" if updated else "noop"}
        return {"status": "noop"}

    return run_async(_run())


@app.task(
    name=_FAILED_TASK,
    bind=True,
    max_retries=TASK_LIMITS[_FAILED_TASK]["retries"],
    acks_late=True,
    soft_time_limit=TASK_LIMITS[_FAILED_TASK]["soft"],
    time_limit=TASK_LIMITS[_FAILED_TASK]["hard"],
)
def mark_salesforce_cdc_repair_failed_task(
    self: Task,
    *args: Any,
    organisation_id: str,
    integration_id: str,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Keep the connector fenced in repairing after any bounded chain phase fails."""
    _ = self
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)
    failed_task_id = str(args[0])[:128] if args else "unknown"
    error = RuntimeError(f"Salesforce CDC repair chain failed at task {failed_task_id}")

    async def _run() -> dict[str, Any]:
        service = SalesforceCdcSubscriptionStateService()
        async for session in acquire_org_async_session(org_id):
            updated = await service.mark_repair_failed(
                session=session,
                organisation_id=org_id,
                integration_id=int_id,
                channel=AGGREGATE_CDC_CHANNEL,
                error=error,
                retry_at=None,
            )
            if updated:
                record_salesforce_cdc_repair("failed", reason="replay_expired")
                logger.error(
                    "Salesforce aggregate CDC repair failed",
                    extra={
                        "event": "salesforce_cdc_repair_failed",
                        "organisation_id": organisation_id,
                        "integration_id": integration_id,
                        "failed_task_id": failed_task_id,
                    },
                )
            return {"status": "repairing" if updated else "noop"}
        return {"status": "noop"}

    return run_async(_run())
