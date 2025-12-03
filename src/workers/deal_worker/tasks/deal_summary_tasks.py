"""Celery tasks for deal-level summary generation.

This module provides the Celery task for processing deal summary jobs.
The task is thin - it delegates to DealSummaryService for business logic.

Deal summaries analyze ALL meeting transcripts for a deal to produce
a comprehensive summary - this is separate from meeting-level tasks.
"""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.services.deal_summary_service import (
    DealSummaryError,
    DealSummaryService,
)

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

        async for session in acquire_org_async_session(org_uuid):
            # 1. Load job
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            publisher = DealJobRedisPublisher(org_uuid, job_uuid, str(job.job_type))

            try:
                # 2. Update status to IN_PROGRESS
                job.status = JobStatus.IN_PROGRESS
                await session.commit()

                # 3. Create service and generate summary
                service = DealSummaryService(session, org_uuid, publisher)
                result = await service.generate_summary(job.deal_id)

                # 4. Save result to job
                job.status = JobStatus.COMPLETED
                job.result_data = result
                await session.commit()

                # 5. Finalize SSE stream
                publisher.publish_result(result)
                publisher.publish_finalize("done")

                logger.info("Deal summary job completed: %s", job_id)
                return {
                    "status": "completed",
                    "job_id": job_id,
                    "job_type": str(job.job_type),
                }

            except DealSummaryError as e:
                # Non-retryable business errors
                logger.info(
                    "Deal summary job failed %s: %s (code=%s, retryable=%s)",
                    job_id,
                    e.message,
                    e.error_code,
                    e.retryable,
                )
                try:
                    job.status = JobStatus.FAILED
                    job.error_message = e.message
                    await session.commit()

                    publisher.publish_error(e.message, e.error_code)
                    publisher.publish_finalize("error")
                except Exception:
                    logger.error("Failed to update job status after error", exc_info=True)

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
                try:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)[:500]
                    job.retry_count += 1
                    await session.commit()

                    publisher.publish_finalize("error")
                except Exception:
                    logger.error("Failed to update job status after error", exc_info=True)
                raise

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
