"""Celery task for post-meeting deal task synchronization."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from celery import Task

from src.agents.workflows.deal_tasks import DealTaskAgent, DealTaskSyncResult
from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)


def _result_to_dict(result: DealTaskSyncResult) -> dict[str, Any]:
    return result.model_dump(mode="json")


@app.task(
    bind=True,
    name="deal_tasks.sync_from_summary",
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
)
def sync_deal_tasks_from_summary(
    self: Task,
    organisation_id: str,
    deal_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Create/complete internal deal tasks from the latest meeting analysis."""
    logger.info(
        "Starting deal task synchronization",
        extra={
            "event": "deal_task_sync_started",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "job_id": job_id,
        },
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)
        job_uuid = UUID(job_id)

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal task sync job {job_id} not found")
            if job.deal_id != deal_uuid:
                raise RuntimeError(f"Deal task sync job {job_id} is not for deal {deal_id}")
            if not job.event_id:
                raise RuntimeError(f"Deal task sync job {job_id} has no source meeting")
            source_event_id = job.event_id
            job_metadata = job.job_metadata or {}
            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        def _metadata_uuid(key: str) -> UUID | None:
            value = job_metadata.get(key)
            try:
                return UUID(str(value)) if value else None
            except ValueError:
                logger.warning("Ignoring invalid %s on deal task job %s", key, job_uuid)
                return None

        agent = DealTaskAgent(
            organisation_id=org_uuid,
            deal_id=deal_uuid,
            source_event_id=source_event_id,
            source_association_id=_metadata_uuid("source_association_id"),
            source_summary_job_id=_metadata_uuid("source_summary_job_id"),
        )
        try:
            result = await agent.sync_from_summary()
        except Exception as exc:
            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = str(exc)[:500]
                    await session.commit()
            raise

        result_data = _result_to_dict(result)
        token_usage = agent.last_token_usage

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = result_data
                if token_usage:
                    job.token_usage = token_usage.to_dict()
                    job.model_name = token_usage.model_name
                    if token_usage.cost is not None:
                        job.cost = Decimal(str(token_usage.cost))
                    if token_usage.duration_seconds is not None:
                        job.latency_ms = int(token_usage.duration_seconds * 1000)
                await session.commit()

        return result_data

    try:
        result = run_async(_run())
        logger.info(
            "Deal task synchronization completed",
            extra={
                "event": "deal_task_sync_completed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "job_id": job_id,
            },
        )
        return result
    except Exception as exc:
        logger.error(
            "Deal task synchronization failed",
            extra={
                "event": "deal_task_sync_failed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "job_id": job_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc


__all__ = ["sync_deal_tasks_from_summary"]
