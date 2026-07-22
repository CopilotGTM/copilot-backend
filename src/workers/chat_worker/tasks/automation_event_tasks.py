"""Celery activation task for directly delivered automation source events."""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from celery import Task

from src.infra.utils.async_runner import run_async
from src.workers.chat_worker.celery_app import app
from src.workers.chat_worker.services.automation_event_activation_service import (
    AutomationEventActivationExecutionService,
)

logger = logging.getLogger(__name__)


@app.task(
    name="chat.activate_automation_event",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def activate_automation_event_task(
    self: Task, organisation_id: str, dispatch_id: str
) -> dict[str, Any]:
    """Activate one dispatch; database idempotency makes redelivery safe."""

    try:
        org_uuid = UUID(organisation_id)
        dispatch_uuid = UUID(dispatch_id)
    except ValueError:
        logger.warning("Discarding automation activation task with invalid UUID payload")
        return {"status": "invalid_payload"}

    result = cast(
        dict[str, Any],
        run_async(
            AutomationEventActivationExecutionService(org_uuid).activate(
                dispatch_uuid,
                retry_allowed=self.request.retries < self.max_retries,
            )
        ),
    )
    if result.get("status") == "retry":
        raise self.retry(countdown=10 * (2**self.request.retries))
    return result
