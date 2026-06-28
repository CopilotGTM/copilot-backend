"""Celery tasks for account-scoped research."""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services.account_research_service import (
    AccountResearchWorkflowError,
    AccountResearchWorkflowService,
)

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="account_research.process_job",
    acks_late=True,
    max_retries=2,
    soft_time_limit=120,
    time_limit=150,
)
def process_account_research_job(self: Task, organisation_id: str, job_id: str):
    """Process one account research job with retry-safe job state updates."""
    lock_key = f"task_lock:account_research:{job_id}"
    if not acquire_task_lock(lock_key, ttl=180):
        return {"status": "skipped", "reason": "already_running"}

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise AccountResearchWorkflowError("Account research job not found", False)

            metadata = job.job_metadata or {}
            company_id_raw = metadata.get("company_id")
            if not company_id_raw:
                raise AccountResearchWorkflowError("Account research job missing company_id", False)

            company_id = UUID(str(company_id_raw))
            job.status = JobStatus.IN_PROGRESS
            await session.commit()
            break

        result = await AccountResearchWorkflowService(org_uuid).research_account(
            company_id,
            job_id=job_uuid,
        )

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = {
                    "provider_status": result.provider_status,
                    "summary_status": result.summary_status,
                    "research_length": len(result.summary_markdown or ""),
                }
                await session.commit()
            break

        return {"status": "completed", "job_id": job_id}

    try:
        return run_async(_run())
    except AccountResearchWorkflowError as exc:
        run_async(_mark_failed(UUID(organisation_id), UUID(job_id), exc.message))
        if not exc.retryable:
            return {"status": "failed", "job_id": job_id, "error": exc.message}
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
        return {"status": "failed", "job_id": job_id, "error": exc.message}
    except Exception as exc:
        logger.exception(
            "Account research task failed",
            extra={
                "event": "account_research_failed",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "failure_reason": str(exc),
            },
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
        run_async(_mark_failed(UUID(organisation_id), UUID(job_id), str(exc)))
        return {"status": "failed", "job_id": job_id, "error": str(exc)}
    finally:
        release_task_lock(lock_key)


async def _mark_failed(organisation_id: UUID, job_id: UUID, message: str) -> None:
    async for session in acquire_org_async_session(organisation_id):
        job = await session.get(DealJobs, job_id)
        if job:
            job.status = JobStatus.FAILED
            job.error_message = message
            await session.commit()
        return
