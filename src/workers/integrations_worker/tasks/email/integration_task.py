"""
Tier 3: Integration-level email sync task.

Syncs a specific email integration for manual/user-level triggers.

This task is used for:
- Frontend "Refresh" button on integrations page
- API endpoint for on-demand sync
- Manual intervention/debugging

NOTE: This task is NOT used by the scheduled sync (Celery Beat).
The scheduler uses Tier 1 → Tier 2, which handles integrations inline.

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

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.email_delta_sync_service import EmailDeltaSyncService
from src.service.integrations.sync_health import record_sync_failure
from src.workers.deal_worker.services.deal_mapper_services.communication_deal_mapping_trigger_service import (
    CommunicationDealMappingTriggerService,
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
) -> dict[str, Any]:
    """
    Tier 3: Sync specific email integration.

    Triggered by: Frontend refresh button, API, manual intervention
    NOT triggered by: Scheduled sync (Tier 1/2)

    This task is for on-demand/manual sync of a single integration.
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
    communication_trigger_source = "webhook" if trigger_source == "webhook" else "manual_sync"

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
            result["communication_deal_mapping"] = (
                await CommunicationDealMappingTriggerService(
                    org_id
                ).trigger_for_changed_communications(
                    integration_id=int_id,
                    source_kind="email",
                    trigger_source=communication_trigger_source,
                    changed_record_ids=result.get("changed_email_ids", []),
                    sync_started_at=started_at_dt,
                )
            ).to_dict()

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
        else:
            logger.warning(
                "Manual email sync failed",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": integration_id,
                    "error": result.get("error"),
                },
            )

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

        # Email sync is retry-safe because message writes are keyed by provider
        # message id; retrying after a transient failure refreshes existing rows.
        if self.request.retries < self.max_retries:
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
