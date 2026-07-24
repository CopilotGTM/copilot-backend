"""
Tier 2: Organization-level email sync task.

Syncs all email integrations for a specific organization.

This task:
1. Queries org DB for all active email integrations
2. For each integration, calls EmailDeltaSyncService
3. Returns org-level aggregated results

Architecture:
    sync_email_all_orgs_task [Tier 1]
        ↓
    sync_email_org_task [Tier 2 - this file] (one per org)
        ↓
    EmailDeltaSyncService._sync_integration() (for each integration, inline)

NOTE: This task uses EmailDeltaSyncService directly (NOT sync_integration_data_task)
because email sync requires:
- Combined folders + messages sync per integration
- Per-folder delta link management
- Different orchestration than calendar

Session Management:
    This task follows the "fresh session" pattern to avoid holding database
    connections unnecessarily:
    1. Session 1: Query integration IDs, release session
    2. For each integration: call EmailDeltaSyncService (which manages its own sessions)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.email_delta_sync_service import (
    EMAIL_INTEGRATION_TYPES,
    EmailDeltaSyncService,
)
from src.service.integrations.sync_health import (
    classify_sync_error,
    is_action_required_failure,
    record_sync_failure,
    record_sync_success,
)
from src.workers.deal_worker.utils.contact_communication_hydration_dispatch import (
    dispatch_contact_communication_hydration_async,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME = "integrations.sync_email_org"
_L = TASK_LIMITS[_TASK_NAME]


@dataclass
class IntegrationInfo:
    """Minimal integration data needed for orchestration."""

    integration_id: UUID
    integration_type: str


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 120),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def sync_email_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    """
    Tier 2: Sync all email integrations for one organization.

    Triggered by: sync_email_all_orgs_task (Tier 1)

    This task loops through all active email integrations for the org
    and calls EmailDeltaSyncService for each one.

    Args:
        organisation_id: The organization UUID string

    Returns:
        Dictionary with org-level sync results:
        {
            "status": "completed",
            "organisation_id": "uuid",
            "started_at": "...",
            "completed_at": "...",
            "summary": {
                "total_integrations": 2,
                "successful": 2,
                "failed": 0,
                "total_folders_synced": 10,
                "total_messages_synced": 250
            },
            "integration_results": [...]
        }
    """
    started_at_dt = datetime.now(UTC)
    started_at = started_at_dt.isoformat()
    org_id = UUID(organisation_id)
    # Track which integration is being synced so the timeout handler can
    # record a failure against the right integration.
    current_integration_id: list[UUID | None] = [None]

    async def _sync_org() -> dict[str, Any]:
        """Sync all email integrations for the organization.

        Uses fresh session pattern:
        1. Session 1: Query integration IDs, release session
        2. For each integration: call EmailDeltaSyncService (manages own sessions)
        """
        logger.info(
            "Email sync starting for org (Tier 2)",
            extra={"organisation_id": organisation_id},
        )

        # Phase 1: Query integration IDs only, then release session
        # EmailDeltaSyncService manages its own sessions for the actual sync
        integration_list: list[IntegrationInfo] = []

        async for session in acquire_org_async_session(org_id):
            stmt = select(
                col(KnowledgeIntegration.integration_id),
                col(KnowledgeIntegration.integration_type),
            ).where(
                col(KnowledgeIntegration.integration_type).in_(
                    EMAIL_INTEGRATION_TYPES + (IntegrationType.GOOGLE, IntegrationType.MICROSOFT)
                ),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(stmt)
            rows = result.all()

            for row in rows:
                int_type = row.integration_type
                integration_list.append(
                    IntegrationInfo(
                        integration_id=row.integration_id,
                        integration_type=(
                            int_type.value if hasattr(int_type, "value") else str(int_type)
                        ),
                    )
                )
        # Session is now released

        if not integration_list:
            logger.info(
                "No active email integrations for org",
                extra={"organisation_id": organisation_id},
            )
            return {
                "status": "completed",
                "organisation_id": organisation_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "total_integrations": 0,
                    "successful": 0,
                    "failed": 0,
                    "action_required": 0,
                    "total_folders_synced": 0,
                    "total_messages_synced": 0,
                },
                "integration_results": [],
            }

        logger.info(
            "Email sync found integrations for org",
            extra={
                "organisation_id": organisation_id,
                "total_integrations": len(integration_list),
            },
        )

        # ─────────────────────────────────────────────────────────────────────
        # Phase 2: Sync each integration (service manages its own sessions)
        # ─────────────────────────────────────────────────────────────────────
        service = EmailDeltaSyncService()
        integration_results = []
        successful = 0
        failed = 0
        action_required = 0
        total_folders = 0
        total_messages = 0

        for info in integration_list:
            current_integration_id[0] = info.integration_id
            logger.info(
                "Syncing email integration",
                extra={
                    "organisation_id": organisation_id,
                    "integration_id": str(info.integration_id),
                    "integration_type": info.integration_type,
                },
            )

            try:
                # Service manages its own database sessions
                sync_result = await service.sync_integration(
                    organisation_id=org_id,
                    integration_id=info.integration_id,
                )

                communication_deal_mapping = None
                if sync_result.get("status") == "success":
                    await record_sync_success(
                        organisation_id=org_id,
                        integration_id=info.integration_id,
                    )
                    if sync_result.get("messages_synced", 0) > 0 or sync_result.get(
                        "changed_email_ids"
                    ):
                        task_ids = await dispatch_contact_communication_hydration_async(
                            organisation_id=org_id,
                            integration_id=info.integration_id,
                            changed_email_ids=sync_result.get("changed_email_ids", []),
                            trigger="deprecated_email_incremental_sync",
                        )
                        communication_deal_mapping = {
                            "status": "triggered",
                            "task_ids": task_ids,
                        }
                    successful += 1
                    total_folders += sync_result.get("folders_synced", 0)
                    total_messages += sync_result.get("messages_synced", 0)
                    result_status = "success"
                else:
                    # Service returned failure in result dict (didn't raise)
                    error_str = sync_result.get("error", "Unknown error")
                    failure_type = classify_sync_error(Exception(error_str))
                    await record_sync_failure(
                        organisation_id=org_id,
                        integration_id=info.integration_id,
                        error=Exception(error_str),
                        failure_type=failure_type,
                    )
                    if is_action_required_failure(failure_type):
                        action_required += 1
                        result_status = "action_required"
                    else:
                        failed += 1
                        result_status = sync_result.get("status", "unknown")

                integration_results.append(
                    {
                        "integration_id": str(info.integration_id),
                        "integration_type": info.integration_type,
                        "status": result_status,
                        "folders_synced": sync_result.get("folders_synced", 0),
                        "messages_synced": sync_result.get("messages_synced", 0),
                        "error": sync_result.get("error"),
                        "failure_type": sync_result.get("error_type"),
                        "communication_deal_mapping": communication_deal_mapping,
                    }
                )

            except Exception as e:
                failure_type = classify_sync_error(e)
                log_extra = {
                    "organisation_id": organisation_id,
                    "integration_id": str(info.integration_id),
                    "error": str(e),
                    "failure_type": failure_type.value,
                }
                if is_action_required_failure(failure_type):
                    logger.warning(
                        "Email integration requires user action",
                        extra=log_extra,
                    )
                else:
                    logger.error(
                        "Failed to sync email integration",
                        exc_info=True,
                        extra=log_extra,
                    )

                await record_sync_failure(
                    organisation_id=org_id,
                    integration_id=info.integration_id,
                    error=e,
                    failure_type=failure_type,
                )

                if is_action_required_failure(failure_type):
                    action_required += 1
                else:
                    failed += 1
                integration_results.append(
                    {
                        "integration_id": str(info.integration_id),
                        "integration_type": info.integration_type,
                        "status": (
                            "action_required"
                            if is_action_required_failure(failure_type)
                            else "failed"
                        ),
                        "error": str(e),
                        "failure_type": failure_type.value,
                    }
                )

        completed_at = datetime.now(UTC).isoformat()

        # Determine overall status
        if failed == 0:
            status = "completed"
        elif successful == 0:
            status = "failed"
        else:
            status = "partial"

        logger.info(
            "Email sync complete for org",
            extra={
                "organisation_id": organisation_id,
                "successful": successful,
                "failed": failed,
                "action_required": action_required,
                "total_folders_synced": total_folders,
                "total_messages_synced": total_messages,
            },
        )

        return {
            "status": status,
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "summary": {
                "total_integrations": len(integration_list),
                "successful": successful,
                "failed": failed,
                "action_required": action_required,
                "total_folders_synced": total_folders,
                "total_messages_synced": total_messages,
            },
            "integration_results": integration_results,
        }

    try:
        return run_async(_sync_org())
    except SoftTimeLimitExceeded:
        logger.error(
            "Email org sync timed out",
            extra={"organisation_id": organisation_id, "soft_limit_seconds": _L["soft"]},
        )
        # Record failure for the integration that was being synced when timeout hit
        int_id = current_integration_id[0]
        if int_id:
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
                    extra={"integration_id": str(int_id)},
                )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded soft time limit of {_L['soft']}s",
        }
    except Exception as e:
        logger.error(
            "Email org sync failed",
            exc_info=True,
            extra={"organisation_id": organisation_id, "error": str(e)},
        )

        # Org fanout can be retried as a unit; per-integration writes are
        # idempotent and failed integrations are recorded individually above.
        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying email org sync",
                extra={
                    "organisation_id": organisation_id,
                    "attempt": self.request.retries + 1,
                    "max_retries": self.max_retries,
                },
            )
            raise self.retry(exc=e) from e

        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
