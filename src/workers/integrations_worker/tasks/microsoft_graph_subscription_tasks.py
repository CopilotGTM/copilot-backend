"""Celery tasks for Microsoft Graph webhook subscription lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.microsoft.subscription_task_service import (
    MicrosoftGraphSubscriptionTaskService,
    kind_for_integration_type,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

ENSURE_TASK = "integrations.ensure_microsoft_graph_subscription"
RENEW_ALL_TASK = "integrations.renew_microsoft_graph_subscriptions_all"

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
def ensure_microsoft_graph_subscription_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """Ensure a Microsoft Graph webhook subscription exists for one integration."""
    # Idempotent task: the service renews an existing subscription or creates a
    # replacement, so retries are safe after worker crashes or Graph 429s.
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    try:
        result = run_async(
            MicrosoftGraphSubscriptionTaskService().ensure_subscription(
                organisation_id=org_id,
                integration_id=int_id,
            )
        )
        result["started_at"] = started_at
        result["completed_at"] = datetime.now(UTC).isoformat()
        return result
    except SoftTimeLimitExceeded:
        logger.error(
            "Microsoft Graph subscription ensure timed out",
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
            "Microsoft Graph subscription ensure failed",
            exc_info=True,
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "error": str(exc),
            },
        )
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying Microsoft Graph subscription ensure",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
            )
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
def renew_microsoft_graph_subscriptions_all_task() -> dict[str, Any]:
    """Dispatch integration-scoped subscription ensure tasks for active Outlook integrations."""
    # Coordinator only: it does not call Graph directly. Each integration-level
    # ensure owns its retry window so one mailbox cannot block renewal fanout.
    started_at = datetime.now(UTC).isoformat()

    def dispatch_subscription_ensure(organisation_id: UUID, integration_id: UUID) -> None:
        """Queue one integration-level ensure from the renewal coordinator."""
        # Use the priority queue because these tasks keep webhook coverage alive;
        # they are lightweight compared with full data sync jobs.
        ensure_microsoft_graph_subscription_task.apply_async(
            kwargs={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            },
            queue="integrations_priority",
            routing_key="integrations_priority",
        )

    try:
        result = run_async(
            MicrosoftGraphSubscriptionTaskService().dispatch_all_subscription_ensures(
                dispatch_subscription_ensure=dispatch_subscription_ensure,
            )
        )
        result["started_at"] = started_at
        result["completed_at"] = datetime.now(UTC).isoformat()
        return result
    except SoftTimeLimitExceeded:
        logger.error(
            "Microsoft Graph subscription renewal dispatch timed out",
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
            "Microsoft Graph subscription renewal dispatch failed",
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


ensure_microsoft_graph_subscription = ensure_microsoft_graph_subscription_task
renew_microsoft_graph_subscriptions_all = renew_microsoft_graph_subscriptions_all_task

__all__ = [
    "ENSURE_TASK",
    "RENEW_ALL_TASK",
    "_kind_for_integration_type",
    "ensure_microsoft_graph_subscription",
    "ensure_microsoft_graph_subscription_task",
    "renew_microsoft_graph_subscriptions_all",
    "renew_microsoft_graph_subscriptions_all_task",
]
