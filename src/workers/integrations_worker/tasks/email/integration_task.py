"""
Tier 3: Integration-level email sync task.

Syncs a specific email integration for manual and webhook triggers.

This task is used for:
- Frontend "Refresh" button on integrations page
- API endpoint for on-demand sync
- Manual intervention/debugging

The webhook recovery watchdog may replay this task, but it does not perform
full scheduled polling; provider cursors remain the source of truth.

Architecture:
    Frontend/API (manual trigger)
        ↓
    sync_email_integration_task [Tier 3 - this file]
        ↓
    EmailDeltaSyncService.sync_integration()
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.domain.models.organisation_tables.sync import SyncStatus
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.email_delta_sync_service import EmailDeltaSyncService
from src.service.integrations.sync_health import (
    classify_sync_error,
    is_action_required_failure,
    record_sync_failure,
)
from src.service.integrations.webhooks.sync_receipt import (
    is_terminal_webhook_result,
    update_webhook_sync_receipt,
)
from src.workers.deal_worker.utils.contact_communication_hydration_dispatch import (
    dispatch_contact_communication_hydration_async,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_email_integration"
_L = TASK_LIMITS[_TASK_NAME]


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_email_integration_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
    trigger_source: str = "manual_sync",
    webhook_sync_id: str | None = None,
) -> dict[str, Any]:
    """
    Tier 3: Sync specific email integration.

    Triggered by: Frontend refresh button, API, webhook delivery/recovery

    This task syncs one integration and is safe for webhook redelivery.
    It provides immediate feedback to the user.

    Args:
        organisation_id: The organization UUID string
        integration_id: The specific integration UUID string

    Returns:
        Dictionary with integration sync results:
        {
            "status": "success",
            "organisation_id": "uuid",
            "integration_id": "uuid",
            "integration_type": "outlook_email",
            "started_at": "...",
            "completed_at": "...",
            "folders_synced": 5,
            "messages_synced": 150,
            "folder_results": [...]
        }
    """
    started_at_dt = datetime.now(UTC)
    started_at = started_at_dt.isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)
    receipt_id = UUID(webhook_sync_id) if webhook_sync_id else None

    def _record_receipt(
        status: SyncStatus,
        *,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> None:
        """Update the durable webhook receipt when this task came from ingress."""
        if receipt_id is None:
            return
        run_async(
            update_webhook_sync_receipt(
                organisation_id=org_id,
                sync_id=receipt_id,
                status=status,
                metadata=metadata,
                error=error,
                completed=completed,
            )
        )

    # Webhook retries share this receipt; provider cursors and message upserts
    # make replay safe even if the broker redelivers the task after a crash.
    _record_receipt(
        SyncStatus.IN_PROGRESS,
        metadata={"sync_attempt": self.request.retries + 1},
    )

    async def _sync_integration() -> dict[str, Any]:
        """Sync the specific integration."""
        logger.info(
            "Manual email sync starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        service = EmailDeltaSyncService()
        result = await service.sync_integration(
            organisation_id=org_id,
            integration_id=int_id,
        )

        if result.get("status") == "success" and (
            result.get("messages_synced", 0) > 0 or result.get("changed_email_ids")
        ):
            # Delta sync commits before returning, so the hydration worker can safely read these
            # IDs. Queue it before deal mapping, which may transfer card ownership to an
            # association-backed projection while both jobs remain replay-safe.
            result[
                "contact_communication_hydration_task_ids"
            ] = await dispatch_contact_communication_hydration_async(
                organisation_id=org_id,
                integration_id=int_id,
                changed_email_ids=result.get("changed_email_ids", []),
                trigger="email_sync",
            )

        # Add timing info to result
        result["started_at"] = started_at
        result["completed_at"] = datetime.now(UTC).isoformat()

        return result

    try:
        result = run_async(_sync_integration())

        # Log based on status
        if result.get("status") == "success":
            logger.info(
                "Manual email sync complete",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "folders_synced": result.get("folders_synced", 0),
                    "messages_synced": result.get("messages_synced", 0),
                },
            )
            _record_receipt(SyncStatus.SUCCESS, completed=True)
        else:
            logger.warning(
                "Manual email sync failed",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": result.get("error"),
                },
            )
            if trigger_source == "webhook":
                if is_terminal_webhook_result(result.get("error")):
                    _record_receipt(
                        SyncStatus.FAILED,
                        metadata={"failure_stage": "worker_result", "terminal": True},
                        error=str(result.get("error") or "Webhook email sync failed"),
                        completed=True,
                    )
                    return result
                raise RuntimeError(str(result.get("error") or "Webhook email sync failed"))

        return result

    except SoftTimeLimitExceeded:
        logger.error(
            "Manual email sync timed out",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "soft_limit_seconds": _L["soft"],
            },
        )
        try:
            run_async(
                record_sync_failure(
                    organisation_id=org_id,
                    integration_id=int_id,
                    error=TimeoutError(f"Soft time limit exceeded ({_L['soft']}s)"),
                )
            )
        except Exception:
            logger.warning(
                "Failed to record timeout failure",
                extra={"integration_id": integration_id},
            )
        _record_receipt(
            SyncStatus.FAILED,
            metadata={"failure_stage": "worker_timeout", "terminal": False},
            error=f"Task exceeded soft time limit of {_L['soft']}s",
            completed=False,
        )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_L['soft']}s",
        }
    except Exception as e:
        logger.error(
            "Manual email sync exception",
            exc_info=True,
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "error": str(e),
            },
        )

        failure_type = classify_sync_error(e)
        terminal = is_action_required_failure(failure_type)

        # Email sync is retry-safe because message writes are keyed by provider
        # message id; retrying after a transient failure refreshes existing rows.
        # Authentication and permission failures cannot recover without the user.
        if not terminal and self.request.retries < self.max_retries:
            _record_receipt(
                SyncStatus.IN_PROGRESS,
                metadata={"retry_scheduled": self.request.retries + 1},
                error=str(e),
            )
            logger.info(
                "Retrying manual email sync",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
            )
            raise self.retry(exc=e) from e

        try:
            run_async(
                record_sync_failure(
                    organisation_id=org_id,
                    integration_id=int_id,
                    error=e,
                )
            )
        except Exception:
            logger.warning(
                "Failed to record terminal email sync failure",
                extra={"integration_id": integration_id},
            )

        _record_receipt(
            SyncStatus.FAILED,
            metadata={
                "failure_stage": "dead_letter",
                "failure_type": failure_type.value,
                "terminal": terminal,
            },
            error=str(e),
            completed=terminal,
        )
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }


# Convenience alias
sync_email_integration = sync_email_integration_task
