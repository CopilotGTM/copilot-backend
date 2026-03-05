"""Celery tasks for deal memory build and update.

This module provides Celery tasks that build and incrementally update
persistent deal memory documents using AI.

Tasks:
    - process_deal_memory_job: Process a deal memory build/update job
    - build_deal_memory_org: Backfill deals without memory for one org (Tier 2)
    - trigger_deal_memory_backfill_all_orgs: Dispatch backfill to all orgs (Tier 1)

The task delegates to DealMemoryService for business logic:
    - Loading existing memory (if any)
    - Calling DealMemoryAgent for generation (agent uses tools to gather data)
    - Saving the updated memory document
    - Backfill: finding deals without memory that have data

Error Handling:
    - DealMemoryError(retryable=False): Deal not found, etc.
    - DealMemoryError(retryable=True): AI generation failed
    - Other errors: Retried with exponential backoff
"""

from __future__ import annotations

from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import (
    Organisation,
    OrganisationStatus,
)
from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.factory import get_async_provider
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services import (
    DealMemoryError,
    DealMemoryService,
)
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="deal_memory.process_job",
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,  # 10 min — reasoning model + many tool calls
    time_limit=660,  # 11 min hard limit
)
def process_deal_memory_job(self: Task, organisation_id: str, job_id: str):
    """Process a deal memory build/update job.

    This Celery task loads the job, delegates to DealMemoryService,
    and handles job status updates and error handling.

    Args:
        organisation_id: The organisation ID (string for JSON serialization)
        job_id: The job ID to process (string for JSON serialization)

    Returns:
        Dictionary with job completion status
    """
    logger.info("Processing deal memory job org=%s job=%s", organisation_id, job_id)

    # Prevent concurrent execution for the same job (retries can stack)
    lock_key = f"task_lock:deal_memory_job:{job_id}"
    if not acquire_task_lock(lock_key, ttl=700):
        logger.info("Deal memory job %s already running, skipping", job_id)
        return {"status": "skipped", "reason": "already_running"}

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Phase 1: Load job context (fresh session)
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            deal_id: UUID | None = job.deal_id
            job_type_str: str = str(job.job_type)
            metadata = job.job_metadata or {}
            trigger_type = metadata.get("trigger_type")
            trigger_source_id_str = metadata.get("trigger_source_id")
            trigger_source_id = UUID(trigger_source_id_str) if trigger_source_id_str else None

            # Mark as IN_PROGRESS
            job.status = JobStatus.IN_PROGRESS
            await session.commit()
            # Session released

        if not deal_id:
            raise RuntimeError(f"Deal job {job_id} has no deal_id")

        # Create publisher for SSE updates
        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str)

        # Phase 2: Call service (service manages its own sessions)
        try:
            service = DealMemoryService(org_uuid, publisher)
            content = await service.build_or_update(
                deal_id,
                trigger_type=trigger_type,
                trigger_source_id=trigger_source_id,
            )
        except Exception as e:
            error_msg = e.message if isinstance(e, DealMemoryError) else str(e)[:500]
            logger.error("Deal memory job failed %s: %s", job_id, e, exc_info=True)

            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = error_msg
                    await session.commit()

            publisher.publish_finalize("error")
            raise

        # Phase 3: Save result (fresh session)
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = {"token_count": len(content.split()) * 2}
                await session.commit()

        publisher.publish_finalize("done")

        logger.info("Deal memory job completed: %s", job_id)
        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": job_type_str,
        }

    try:
        return run_async(_run())
    except Exception as e:
        logger.error("Deal memory task failed %s: %s", job_id, e, exc_info=True)
        # Exponential backoff: 60, 120, 240 seconds
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
    finally:
        release_task_lock(lock_key)


@app.task(bind=True, name="deal_memory.backfill_org", acks_late=True, max_retries=2)
def build_deal_memory_org(self: Task, organisation_id: str):
    """Tier 2: Backfill deals without memory for one org.

    Delegates to DealMemoryService.backfill_org() which finds open deals
    without memory that have data (meetings, emails, or notes), creates
    DealJobs for them. This task then dispatches process_deal_memory_job
    for each created job.

    Args:
        organisation_id: The organisation ID (string for JSON serialization)

    Returns:
        Dictionary with dispatch counts
    """
    lock_key = f"task_lock:deal_memory_backfill:{organisation_id}"
    if not acquire_task_lock(lock_key, ttl=1800):
        return {"status": "skipped", "reason": "already_running"}

    try:
        service = DealMemoryService(UUID(organisation_id))
        result = run_async(service.backfill_org())

        # Dispatch process jobs for each created job
        for org_id_str, job_id_str in result["jobs_to_dispatch"]:
            process_deal_memory_job.delay(organisation_id=org_id_str, job_id=job_id_str)

        logger.info(
            "Deal memory backfill org=%s dispatched=%d skipped=%d",
            organisation_id,
            result["dispatched"],
            result["skipped"],
        )
        return {
            "status": "completed",
            "dispatched": result["dispatched"],
            "skipped": result["skipped"],
        }
    except Exception as exc:
        logger.error("Backfill failed for org %s: %s", organisation_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=120) from exc
    finally:
        release_task_lock(lock_key)


@app.task(bind=False, name="deal_memory.trigger_backfill_all_orgs", acks_late=True, max_retries=0)
def trigger_deal_memory_backfill_all_orgs():
    """Tier 1: Dispatch backfill for all approved organisations.

    Returns:
        Dictionary with dispatch count
    """

    async def _dispatch():
        provider = get_async_provider()
        async with provider.master_session() as master_session:
            result = await master_session.execute(
                select(Organisation).where(col(Organisation.status) == OrganisationStatus.APPROVED)
            )
            orgs = result.scalars().all()

        dispatched = 0
        for org in orgs:
            try:
                build_deal_memory_org.delay(organisation_id=str(org.organisation_id))
                dispatched += 1
            except Exception as e:
                logger.warning("Failed to dispatch for org %s: %s", org.organisation_id, e)

        return {"status": "completed", "orgs_dispatched": dispatched}

    return run_async(_dispatch())
