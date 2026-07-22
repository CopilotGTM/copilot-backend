"""Celery tasks for Microsoft Graph webhook subscription lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.domain.models.organisation_tables.sync import SyncStatus
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.sync_health import classify_sync_error, is_action_required_failure
from src.service.integrations.webhooks.microsoft.metadata import MicrosoftSubscriptionKind
from src.service.integrations.webhooks.microsoft.subscription_task_service import (
    MicrosoftGraphSubscriptionTaskService,
    kind_for_integration_type,
)
from src.service.integrations.webhooks.sync_receipt import update_webhook_sync_receipt
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task
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
    kind: str | None = None,
    lifecycle_event: str | None = None,
    sync_after: bool = False,
    webhook_sync_id: str | None = None,
    trigger_source: str | None = None,
) -> dict[str, Any]:
    """Ensure a Microsoft Graph webhook subscription exists for one integration."""
    # Idempotent task: the service renews an existing subscription or creates a
    # replacement, so retries are safe after worker crashes or Graph 429s.
    started_at = datetime.now(UTC).isoformat()
    _ = trigger_source
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    try:
        requested_kind = MicrosoftSubscriptionKind(kind) if kind else None
        result = run_async(
            MicrosoftGraphSubscriptionTaskService().ensure_subscription(
                organisation_id=org_id,
                integration_id=int_id,
                requested_kind=requested_kind,
                lifecycle_event=lifecycle_event,
            )
        )
        if result.get("status") == "skipped" and webhook_sync_id:
            run_async(
                update_webhook_sync_receipt(
                    organisation_id=org_id,
                    sync_id=UUID(webhook_sync_id),
                    status=SyncStatus.FAILED,
                    metadata={
                        "failure_stage": "subscription_recovery_result",
                        "terminal": True,
                    },
                    error=str(result.get("reason") or "Integration is unavailable"),
                    completed=True,
                )
            )
        elif (
            sync_after
            and requested_kind is not None
            and requested_kind.value in result.get("kinds", [])
        ):
            task_name = (
                "integrations.sync_calendar_integration"
                if requested_kind == MicrosoftSubscriptionKind.CALENDAR
                else "integrations.sync_email_integration"
            )
            send_integrations_task(
                task_name=task_name,
                kwargs={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "trigger_source": "webhook",
                    "webhook_sync_id": webhook_sync_id,
                },
                queue="integrations_priority",
                routing_key="integrations_priority",
            )
        elif webhook_sync_id:
            run_async(
                update_webhook_sync_receipt(
                    organisation_id=org_id,
                    sync_id=UUID(webhook_sync_id),
                    status=SyncStatus.SUCCESS,
                    metadata={"subscription_recovered": True},
                    completed=True,
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
        if webhook_sync_id:
            run_async(
                update_webhook_sync_receipt(
                    organisation_id=org_id,
                    sync_id=UUID(webhook_sync_id),
                    status=SyncStatus.FAILED,
                    metadata={
                        "failure_stage": "subscription_recovery_timeout",
                        "terminal": False,
                    },
                    error="Microsoft Graph subscription recovery timed out",
                    completed=False,
                )
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
        failure_type = classify_sync_error(exc)
        terminal = is_action_required_failure(failure_type)
        if not terminal and self.request.retries < self.max_retries:
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

        if webhook_sync_id:
            run_async(
                update_webhook_sync_receipt(
                    organisation_id=org_id,
                    sync_id=UUID(webhook_sync_id),
                    status=SyncStatus.FAILED,
                    metadata={
                        "failure_stage": "subscription_recovery",
                        "failure_type": failure_type.value,
                        "terminal": terminal,
                    },
                    error=str(exc),
                    completed=terminal,
                )
            )

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
