"""Scheduled Sales Dashboard snapshot refresh tasks."""

from __future__ import annotations

from time import perf_counter
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.dashboard.dashboard_snapshot_service import DashboardSnapshotService
from src.workers.deal_worker.celery_app import app

logger = get_logger("dashboard_snapshot_tasks")

# copilotgtm: mirrors the one-off org migration quarantine; replace with DB-backed
# migration eligibility if more dashboard-skipped orgs appear.
DASHBOARD_SNAPSHOT_EXCLUDED_ORG_IDS = {UUID("6fc45305-2989-44ae-9cc1-a55f39450a9e")}


@app.task(
    name="dashboard.refresh_sales_snapshots_all_orgs",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
)
def refresh_sales_snapshots_all_orgs_task() -> dict[str, Any]:
    """Dispatch one Sales Dashboard snapshot refresh task per approved organisation."""

    async def _dispatch_all_orgs() -> dict[str, Any]:
        org_ids: list[UUID] = []
        async for master_session in acquire_master_async_session():
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) == OrganisationStatus.APPROVED
            )
            result = await master_session.execute(stmt)
            org_ids = [
                row[0] for row in result.all() if row[0] not in DASHBOARD_SNAPSHOT_EXCLUDED_ORG_IDS
            ]

        org_tasks = []
        for org_id in org_ids:
            try:
                task = refresh_sales_snapshot_org_task.apply_async(
                    kwargs={"organisation_id": str(org_id)},
                    queue="deal",
                    headers=correlation_headers(),
                )
                org_tasks.append({"org_id": str(org_id), "task_id": task.id})
            except Exception:
                logger.exception(
                    "Failed to dispatch Sales Dashboard snapshot refresh",
                    extra={"organisation_id": str(org_id)},
                )

        return {
            "status": "completed",
            "orgs_found": len(org_ids),
            "orgs_triggered": len(org_tasks),
            "org_tasks": org_tasks,
        }

    return run_async(_dispatch_all_orgs())


@app.task(
    name="dashboard.refresh_sales_snapshot_org",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=600,
    time_limit=720,
)
def refresh_sales_snapshot_org_task(self: Task, organisation_id: str) -> dict[str, Any]:
    """Render and persist the Sales Dashboard snapshot for one organisation."""

    async def _refresh() -> None:
        org_uuid = UUID(organisation_id)
        async for master_session in acquire_master_async_session():
            async for org_session in acquire_org_async_session(org_uuid):
                await DashboardSnapshotService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=org_uuid,
                ).regenerate_sales_snapshot(user_id=None)

    started_at = perf_counter()
    try:
        run_async(_refresh())
        duration_seconds = round(perf_counter() - started_at, 3)
        logger.info(
            "Sales Dashboard snapshot refresh completed",
            extra={
                "organisation_id": organisation_id,
                "dashboard_key": "sales",
                "status": "success",
                "duration_seconds": duration_seconds,
            },
        )
        return {"status": "success", "organisation_id": organisation_id}
    except Exception as exc:
        duration_seconds = round(perf_counter() - started_at, 3)
        logger.exception(
            "Sales Dashboard snapshot refresh failed",
            extra={
                "organisation_id": organisation_id,
                "dashboard_key": "sales",
                "status": "failed",
                "duration_seconds": duration_seconds,
                "error": str(exc),
            },
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
        return {"status": "failed", "organisation_id": organisation_id, "error": str(exc)}
