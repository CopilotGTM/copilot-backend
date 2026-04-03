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

logger = get_logger(__name__)


@app.task(
    name="integrations.refresh_tokens",
    bind=False,
    max_retries=0,
    acks_late=True,
)
def refresh_tokens_task(buffer_hours: int = 2) -> dict[str, Any]:
    """
    Tier 1: Dispatch token refresh for all approved organizations.

    Triggered by: Celery Beat (scheduled every 30 minutes)
    Dispatches: refresh_tokens_org_task for each approved organization
    """
    started_at = datetime.now(UTC).isoformat()

    async def _dispatch_all_orgs() -> dict[str, Any]:
        logger.info(
            "Starting token refresh for all organizations (Tier 1), buffer=%dh", buffer_hours
        )

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

        logger.info("Found %d approved organization(s) for token refresh", len(org_ids))

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
                logger.error("Failed to dispatch token refresh for org %s: %s", org_id, str(e))
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
    except Exception as e:
        logger.error("Token refresh all-orgs task failed: error=%s", str(e), exc_info=True)
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }


@app.task(
    name="integrations.refresh_tokens_org",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    acks_late=True,
    soft_time_limit=300,
    time_limit=330,
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
            "Starting token refresh for organization (Tier 2): %s, buffer=%dh", org_id, buffer_hours
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
    except Exception as e:
        logger.error(
            "Token refresh org task failed: org=%s error=%s", organisation_id, str(e), exc_info=True
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


@app.task(
    name="integrations.refresh_token",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=120,
    time_limit=150,
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
        logger.info("Starting manual token refresh (Tier 3): org=%s integration=%s", org_id, int_id)

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
    except Exception as e:
        logger.error(
            "Token refresh failed: org=%s integration=%s error=%s",
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
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
