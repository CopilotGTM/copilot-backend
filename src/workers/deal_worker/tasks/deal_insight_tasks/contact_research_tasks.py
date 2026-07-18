"""Celery tasks for contact-scoped research.

Contact research uses provider-first enrichment and a no-tool summary step.
Jobs are contact-scoped but reuse DealJobs so existing job visibility and retry
state stay in one place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services.contact_research_service import (
    ContactResearchWorkflowError,
    ContactResearchWorkflowService,
)

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="contact_research.process_job",
    acks_late=True,
    max_retries=2,
    soft_time_limit=120,
    time_limit=150,
)
def process_contact_research_job(self: Task, organisation_id: str, job_id: str):
    """Process one contact research job with retry-safe job state updates."""
    lock_key = f"task_lock:contact_research:{job_id}"
    if not acquire_task_lock(lock_key, ttl=180):
        return {"status": "skipped", "reason": "already_running"}

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise ContactResearchWorkflowError("Contact research job not found", False)

            metadata = job.job_metadata or {}
            contact_id_raw = metadata.get("contact_id")
            if not contact_id_raw:
                raise ContactResearchWorkflowError("Contact research job missing contact_id", False)

            contact_id = UUID(str(contact_id_raw))
            force_refresh = bool(metadata.get("force_refresh"))
            job.status = JobStatus.IN_PROGRESS
            # Refresh the recovery lease on every Celery attempt. The status may already be
            # in_progress, so assigning it alone would not advance updated_at on retries.
            job.updated_at = datetime.now(UTC)
            await session.commit()
            break

        result = await ContactResearchWorkflowService(org_uuid).research_contact(
            contact_id,
            job_id=job_uuid,
            force_refresh=force_refresh,
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
    except ContactResearchWorkflowError as exc:
        if not exc.retryable:
            run_async(_mark_failed(UUID(organisation_id), UUID(job_id), exc.message))
            return {"status": "failed", "job_id": job_id, "error": exc.message}
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
        run_async(_mark_failed(UUID(organisation_id), UUID(job_id), exc.message))
        return {"status": "failed", "job_id": job_id, "error": exc.message}
    except Exception as exc:
        logger.exception(
            "Contact research task failed",
            extra={
                "event": "contact_research_failed",
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
