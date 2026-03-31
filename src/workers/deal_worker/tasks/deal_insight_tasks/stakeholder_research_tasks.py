"""Celery task for stakeholder research via LLM agent.

Processes a single stakeholder research job:
1. Load DealJobs record, extract stakeholder_id, mark IN_PROGRESS
2. Call StakeholderResearchService.research_stakeholder()
3. Save result + token_usage to DealJobs, mark COMPLETED
"""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services import (
    StakeholderResearchError,
    StakeholderResearchService,
)

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="stakeholder_research.process_job",
    acks_late=True,
    max_retries=2,
    soft_time_limit=120,
    time_limit=150,
)
def process_stakeholder_research_job(self: Task, organisation_id: str, job_id: str):
    """Research a single stakeholder via LLM agent.

    Args:
        organisation_id: The organisation ID (string for JSON serialization)
        job_id: The DealJobs ID to process
    """
    logger.info(
        "Stakeholder research job started",
        extra={
            "event": "stakeholder_research_started",
            "organisation_id": organisation_id,
            "job_id": job_id,
        },
    )

    lock_key = f"task_lock:stakeholder_research:{job_id}"
    if not acquire_task_lock(lock_key, ttl=180):
        logger.info(
            "Stakeholder research job skipped",
            extra={
                "event": "stakeholder_research_skipped",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "skip_reason": "already_running",
            },
        )
        return {"status": "skipped", "reason": "already_running"}

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Phase 1: Load job context (fresh session)
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Stakeholder research job {job_id} not found")

            stakeholder_id: UUID | None = job.stakeholder_id
            deal_id: UUID | None = job.deal_id

            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        if not stakeholder_id:
            raise RuntimeError(f"Stakeholder research job {job_id} has no stakeholder_id")

        # Phase 2: Call service
        service = StakeholderResearchService(org_uuid)
        try:
            markdown = await service.research_stakeholder(
                stakeholder_id=stakeholder_id,
                deal_id=deal_id,
            )
        except StakeholderResearchError as e:
            logger.error(
                "Stakeholder research failed",
                extra={
                    "event": "stakeholder_research_failed",
                    "organisation_id": organisation_id,
                    "job_id": job_id,
                    "stakeholder_id": str(stakeholder_id),
                    "deal_id": str(deal_id) if deal_id else None,
                    "failure_reason": e.message,
                },
            )
            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = e.message
                    await session.commit()
            if not e.retryable:
                return {"status": "failed", "error": e.message}
            raise

        # Phase 3: Save result + token usage (fresh session)
        token_usage = service.last_token_usage
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = {"research_length": len(markdown)}
                if token_usage:
                    job.token_usage = token_usage.to_dict()
                    job.model_name = token_usage.model_name
                    if token_usage.cost is not None:
                        from decimal import Decimal

                        job.cost = Decimal(str(token_usage.cost))
                    if token_usage.duration_seconds is not None:
                        job.latency_ms = int(token_usage.duration_seconds * 1000)
                await session.commit()

        logger.info(
            "Stakeholder research completed",
            extra={
                "event": "stakeholder_research_completed",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "stakeholder_id": str(stakeholder_id),
                "deal_id": str(deal_id) if deal_id else None,
            },
        )
        return {"status": "completed", "job_id": job_id}

    try:
        return run_async(_run())
    except Exception as e:
        logger.error(
            "Stakeholder research task failed",
            extra={
                "event": "stakeholder_research_failed",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        raise self.retry(exc=e, countdown=30 * (2**self.request.retries)) from e
    finally:
        release_task_lock(lock_key)
