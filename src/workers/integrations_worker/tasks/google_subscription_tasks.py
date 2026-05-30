"""Celery task adapters for Google webhook subscription lifecycle.

The service layer owns provider behavior and DB sessions; this module owns
Celery naming, retry limits, and queue placement so other code can dispatch by
stable task name without importing provider clients.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.google.subscription_task_service import (
    GoogleSubscriptionTaskService,
    kind_for_integration_type,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

ENSURE_TASK = "integrations.ensure_google_subscription"
RENEW_ALL_TASK = "integrations.renew_google_subscriptions_all"

_ENSURE_LIMITS = TASK_LIMITS[ENSURE_TASK]
_RENEW_ALL_LIMITS = TASK_LIMITS[RENEW_ALL_TASK]
_kind_for_integration_type = kind_for_integration_type


@app.task(
    name=ENSURE_TASK,
    bind=True,
    max_retries=_ENSURE_LIMITS["retries"],
    default_retry_delay=_ENSURE_LIMITS.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_ENSURE_LIMITS["soft"],
    time_limit=_ENSURE_LIMITS["hard"],
)
def ensure_google_subscription_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Ensure a Google webhook subscription exists for one integration.

    Safe to retry: the service reuses fresh unexpired metadata and rechecks the
    integration is still active before persisting provider responses.
    """
    started_at = datetime.now(UTC).isoformat()
    try:
        result = run_async(
            GoogleSubscriptionTaskService().ensure_subscription(
                organisation_id=UUID(organisation_id),
                integration_id=UUID(integration_id),
            )
        )
        result["started_at"] = started_at
        result["completed_at"] = datetime.now(UTC).isoformat()
        return result
    except SoftTimeLimitExceeded:
        # Soft timeouts return a structured result instead of retrying blindly;
        # the next scheduled renewal can pick up the integration if needed.
        logger.error(
            "Google subscription ensure timed out",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "soft_limit_seconds": _ENSURE_LIMITS["soft"],
            },
        )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_ENSURE_LIMITS['soft']}s",
        }
    except Exception as exc:
        logger.error(
            "Google subscription ensure failed",
            exc_info=True,
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "error": str(exc),
            },
        )
        if self.request.retries < self.max_retries:
            # Subscription creation is idempotent at our metadata layer, so
            # transient Google or token-refresh errors can use Celery retries.
            raise self.retry(exc=exc) from exc
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


@app.task(
    name=RENEW_ALL_TASK,
    bind=False,
    max_retries=_RENEW_ALL_LIMITS["retries"],
    acks_late=True,
    soft_time_limit=_RENEW_ALL_LIMITS["soft"],
    time_limit=_RENEW_ALL_LIMITS["hard"],
)
def renew_google_subscriptions_all_task() -> dict[str, Any]:
    """Dispatch integration-scoped subscription ensure tasks for Google integrations.

    This coordinator intentionally does not call Google APIs. It only fans out
    per-integration work so retries, timeouts, and queue pressure stay isolated.
    """
    started_at = datetime.now(UTC).isoformat()

    def dispatch_subscription_ensure(organisation_id: UUID, integration_id: UUID) -> None:
        """Enqueue one integration-scoped ensure on the priority queue."""
        # Renewal work should not wait behind bulk sync jobs; each ensure is
        # small and protects webhook freshness for one connected integration.
        ensure_google_subscription_task.apply_async(
            kwargs={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            },
            queue="integrations_priority",
            routing_key="integrations_priority",
        )

    try:
        result = run_async(
            GoogleSubscriptionTaskService().dispatch_all_subscription_ensures(
                dispatch_subscription_ensure=dispatch_subscription_ensure,
            )
        )
        result["started_at"] = started_at
        result["completed_at"] = datetime.now(UTC).isoformat()
        return result
    except SoftTimeLimitExceeded:
        logger.error(
            "Google subscription renewal dispatch timed out",
            extra={"soft_limit_seconds": _RENEW_ALL_LIMITS["soft"]},
        )
        return {
            "status": "timed_out",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_RENEW_ALL_LIMITS['soft']}s",
        }
    except Exception as exc:
        logger.error(
            "Google subscription renewal dispatch failed",
            exc_info=True,
            extra={"error": str(exc)},
        )
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


ensure_google_subscription = ensure_google_subscription_task
renew_google_subscriptions_all = renew_google_subscriptions_all_task

__all__ = [
    "ENSURE_TASK",
    "RENEW_ALL_TASK",
    "_kind_for_integration_type",
    "ensure_google_subscription",
    "ensure_google_subscription_task",
    "renew_google_subscriptions_all",
    "renew_google_subscriptions_all_task",
]
