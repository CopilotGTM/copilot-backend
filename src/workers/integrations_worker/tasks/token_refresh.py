"""
Celery tasks for integration token refresh operations.

Three-tier architecture:
    Celery Beat (every 30 min)
        |
    refresh_tokens_task [Tier 1 - all orgs]
        |
    refresh_tokens_org_task [Tier 2] (one per org, calls service)
        |
    refresh_token_task [Tier 3] (manual/on-demand only)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

# from src.service.integrations.token_refresh_service import TokenRefreshService
from src.service.integrations.custom_token_refresh_service import CustomTokenRefreshService
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_TASK_NAME_ALL = "integrations.refresh_tokens"
_L_ALL = TASK_LIMITS[_TASK_NAME_ALL]


@app.task(
    name=_TASK_NAME_ALL,
    bind=False,
    max_retries=_L_ALL["retries"],
    acks_late=True,
    soft_time_limit=_L_ALL["soft"],
    time_limit=_L_ALL["hard"],
)
def refresh_tokens_task(buffer_hours: int = 2) -> dict[str, Any]:
    """
    Tier 1: Dispatch token refresh for all approved organizations.

    Triggered by: Celery Beat (scheduled every 4 hours)
    Dispatches: refresh_tokens_org_task for each approved organization
    """
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        logger.info("Token refresh starting (Tier 1)", extra={"buffer_hours": buffer_hours})

        # Query all approved organizations
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [row[0] for row in result.all()]

        if not org_ids:
            return {
                "status": "completed",
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": {"total_orgs": 0, "orgs_triggered": 0, "orgs_skipped": 0},
                "org_tasks": [],
            }

        logger.info("Token refresh dispatching orgs", extra={"total_orgs": len(org_ids)})

        # Dispatch Tier 2 for each organization
        org_tasks = []
        orgs_triggered = 0
        orgs_skipped = 0

        for org_id in org_ids:
            try:
                task = refresh_tokens_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id), "buffer_hours": buffer_hours},
                    headers=correlation_headers(),
                )
                org_tasks.append({"org_id": str(org_id), "task_id": task.id, "status": "triggered"})
                orgs_triggered += 1
            except Exception as e:
                logger.error(
                    "Failed to dispatch token refresh for org",
                    extra={"organisation_id": str(org_id), "error": str(e)},
                )
                org_tasks.append({"org_id": str(org_id), "status": "failed", "error": str(e)})
                orgs_skipped += 1

        return {
            "status": "completed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total_orgs": len(org_ids),
                "orgs_triggered": orgs_triggered,
                "orgs_skipped": orgs_skipped,
            },
            "org_tasks": org_tasks,
        }

    try:
        return run_async(_dispatch_all_orgs())
    except SoftTimeLimitExceeded:
        logger.error(
            "Token refresh coordinator timed out",
            extra={"soft_limit_seconds": _L_ALL["soft"]},
        )
        return {"status": "timed_out", "error": f"Task exceeded {_L_ALL['soft']}s soft limit"}
    except Exception as e:
        logger.error("Token refresh all-orgs task failed", exc_info=True, extra={"error": str(e)})
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }


_TASK_NAME_ORG = "integrations.refresh_tokens_org"
_L_ORG = TASK_LIMITS[_TASK_NAME_ORG]


@app.task(
    name=_TASK_NAME_ORG,
    bind=True,
    max_retries=_L_ORG["retries"],
    default_retry_delay=_L_ORG["retry_delay"],
    acks_late=True,
    soft_time_limit=_L_ORG["soft"],
    time_limit=_L_ORG["hard"],
)
def refresh_tokens_org_task(
    self: Task,
    organisation_id: str,
    buffer_hours: int = 2,
) -> dict[str, Any]:
    """
    Tier 2: Refresh tokens needing refresh in one organization.

    Refreshes tokens that have no expiry set (NULL) or are expiring soon.

    Triggered by: refresh_tokens_task (Tier 1)
    Calls: CustomTokenRefreshService which manages its own sessions (fresh session pattern)
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)

    async def _refresh_org() -> dict[str, Any]:
        logger.info(
            "Token refresh starting for org (Tier 2)",
            extra={"organisation_id": organisation_id, "buffer_hours": buffer_hours},
        )

        # Org-level: CustomTokenRefreshService manages its own sessions (fresh session pattern)
        # service = TokenRefreshService(organisation_id=org_id)
        service = CustomTokenRefreshService(organisation_id=org_id)
        result = await service.refresh_org_tokens_async(buffer_hours=buffer_hours)

        return {
            "status": result.get("status", "completed"),
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **result,
        }

    try:
        return run_async(_refresh_org())
    except SoftTimeLimitExceeded:
        logger.error(
            "Token refresh org task timed out",
            extra={"organisation_id": organisation_id, "soft_limit_seconds": _L_ORG["soft"]},
        )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded {_L_ORG['soft']}s soft limit",
        }
    except Exception as e:
        logger.error(
            "Token refresh org task failed",
            exc_info=True,
            extra={"organisation_id": organisation_id, "error": str(e)},
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e) from e
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }


_TASK_NAME_SINGLE = "integrations.refresh_token"
_L_SINGLE = TASK_LIMITS[_TASK_NAME_SINGLE]


@app.task(
    name=_TASK_NAME_SINGLE,
    bind=True,
    max_retries=_L_SINGLE["retries"],
    default_retry_delay=_L_SINGLE["retry_delay"],
    acks_late=True,
    soft_time_limit=_L_SINGLE["soft"],
    time_limit=_L_SINGLE["hard"],
)
def refresh_token_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    """
    Tier 3: Refresh token for a single integration (manual/on-demand only).

    This task is NOT called by the scheduled flow. Use it for:
    - Manual refresh from API endpoint
    - On-demand refresh triggered by other services
    """
    started_at = datetime.now(UTC).isoformat()
    org_id = UUID(organisation_id)
    int_id = UUID(integration_id)

    async def _refresh_integration() -> dict[str, Any]:
        logger.info(
            "Manual token refresh starting (Tier 3)",
            extra={"organisation_id": organisation_id, "integration_id": integration_id},
        )

        # Service manages its own session (fresh session pattern)
        # service = TokenRefreshService(organisation_id=org_id)
        service = CustomTokenRefreshService(organisation_id=org_id)
        result = await service.refresh_single_token_async(integration_id=int_id)

        return {
            "status": "success" if result["success"] else "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            **result,
        }

    try:
        return run_async(_refresh_integration())
    except SoftTimeLimitExceeded:
        logger.error(
            "Token refresh task timed out",
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "soft_limit_seconds": _L_SINGLE["soft"],
            },
        )
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"Task exceeded {_L_SINGLE['soft']}s soft limit",
        }
    except Exception as e:
        logger.error(
            "Token refresh failed",
            exc_info=True,
            extra={
                "organisation_id": organisation_id,
                "integration_id": integration_id,
                "error": str(e),
            },
        )
        if self.request.retries < self.max_retries:
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
