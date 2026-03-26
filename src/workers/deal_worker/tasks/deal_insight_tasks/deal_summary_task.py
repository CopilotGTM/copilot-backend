"""Celery tasks for AI-powered deal summary generation.

This module provides Celery tasks that generate comprehensive deal summaries
by analyzing ALL meeting transcripts associated with a deal using AI.

Tasks:
    - process_deal_summary_job: Process a deal summary job from the job queue

The task delegates to DealSummaryService for business logic:
    - Loading deal context and stakeholders
    - Fetching transcripts via TranscriptService
    - Building aggregated context for the AI agent
    - Calling DealSummaryAgent for generation
    - Publishing progress updates via SSE (Redis)

Error Handling:
    - DealNotFoundError: Deal doesn't exist (non-retryable)
    - NoTranscriptsError: No transcripts available (non-retryable)
    - Other errors: Retried with exponential backoff

See docs/Technical Documentation/deal/deal-worker-tasks.md for full documentation.
"""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services import (
    DealSummaryError,
    DealSummaryService,
)
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


@app.task(bind=True, name="deal_summary_task.process", acks_late=True, max_retries=3)
def process_deal_summary_job(self: Task, organisation_id: str, job_id: str):
    """Process a deal summary job.

    This Celery task loads the job, delegates to DealSummaryService,
    and handles job status updates and error handling.

    Args:
        organisation_id: The organisation ID (string for JSON serialization)
        job_id: The job ID to process (string for JSON serialization)

    Returns:
        Dictionary with job completion status
    """
    logger.info("Processing deal summary job org=%s job=%s", organisation_id, job_id)

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

            # Mark as IN_PROGRESS
            job.status = JobStatus.IN_PROGRESS
            await session.commit()
            # Session released

        if not deal_id:
            raise RuntimeError(f"Deal job {job_id} has no deal_id")

        # Create publisher for SSE updates
        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str)

        # Phase 2: Call service (service manages its own sessions)
        service = DealSummaryService(org_uuid, publisher)
        try:
            result = await service.generate_summary(deal_id)

        except DealSummaryError as e:
            # Non-retryable business errors
            logger.info(
                "Deal summary job failed %s: %s (code=%s, retryable=%s)",
                job_id,
                e.message,
                e.error_code,
                e.retryable,
            )

            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = e.message
                    await session.commit()

            publisher.publish_error(e.message, e.error_code)
            publisher.publish_finalize("error")

            if e.retryable:
                raise

            return {
                "status": "failed",
                "job_id": job_id,
                "error": e.message,
                "error_code": e.error_code,
                "retryable": False,
            }

        except Exception as e:
            logger.error("Deal summary job failed %s: %s", job_id, e, exc_info=True)

            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)[:500]
                    job.retry_count += 1
                    await session.commit()

            publisher.publish_finalize("error")
            raise

        # Phase 3: Save result and costing (fresh session)
        token_usage = service.last_token_usage
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = result
                if token_usage:
                    job.token_usage = token_usage.to_dict()
                    job.model_name = token_usage.model_name
                    if token_usage.cost is not None:
                        from decimal import Decimal

                        job.cost = Decimal(str(token_usage.cost))
                    if token_usage.duration_seconds is not None:
                        job.latency_ms = int(token_usage.duration_seconds * 1000)
                await session.commit()

        publisher.publish_result(result)
        publisher.publish_finalize("done")

        logger.info("Deal summary job completed: %s", job_id)
        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": job_type_str,
        }

    try:
        return run_async(_run())
    except DealSummaryError as e:
        if not e.retryable:
            return {
                "status": "failed",
                "job_id": job_id,
                "error": e.message,
                "retryable": False,
            }
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
    except Exception as e:
        logger.error("Deal summary task failed %s: %s", job_id, e, exc_info=True)
        # Exponential backoff: 60, 120, 240 seconds
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
