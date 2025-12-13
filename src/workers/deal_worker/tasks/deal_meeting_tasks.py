"""Celery tasks for deal meeting job processing.

This module provides the main Celery task for processing deal meeting jobs:
- MEETING_BRIEFING: Pre-meeting preparation briefing
- MEETING_SUMMARY: Post-meeting summary generation (requires transcript)
- MEETING_FOLLOWUP: Follow-up action items (future)

Job handlers are defined in the services module for better separation of concerns.
"""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_job_handlers import (
    NonRetryableJobError,
    get_job_handler,
)
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


@app.task(bind=True, name="deal_meeting_task.process", acks_late=True, max_retries=3)
def process_deal_meeting_job(self: Task, organisation_id: str, job_id: str):
    """Process a deal meeting job.

    This is the main Celery task for processing deal meeting jobs.
    It dispatches to specific handlers based on job_type.

    Args:
        organisation_id: The organisation ID (as string for JSON serialization)
        job_id: The job ID to process (as string for JSON serialization)

    Returns:
        Dictionary with job completion status
    """
    logger.info("Processing deal meeting job org=%s job=%s", organisation_id, job_id)

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Use async for pattern for proper session lifecycle
        async for session in acquire_org_async_session(org_uuid):
            # 1. Load job
            logger.info("Loading deal meeting job %s", job_id)
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            publisher = DealJobRedisPublisher(org_uuid, job_uuid, str(job.job_type))

            try:
                # 2. Update status to IN_PROGRESS
                job.status = JobStatus.IN_PROGRESS
                await session.commit()

                # 3. Dispatch to handler based on job type
                handler = get_job_handler(job.job_type)
                logger.info("Dispatching to handler for job %s type=%s", job_id, job.job_type)
                result = await handler(session, job, publisher)

                # 4. Save result
                job.status = JobStatus.COMPLETED
                job.result_data = result
                await session.commit()

                # 5. Finalize SSE stream
                publisher.publish_result(result)
                publisher.publish_finalize("done")

                logger.info("Deal meeting job completed: %s type=%s", job_id, job.job_type)
                return {
                    "status": "completed",
                    "job_id": job_id,
                    "job_type": str(job.job_type),
                }

            except NonRetryableJobError as e:
                # Permanent failure - don't retry, complete the stream gracefully
                logger.info(
                    "Deal meeting job permanently failed %s: %s (code=%s)",
                    job_id,
                    str(e),
                    e.error_code,
                )
                try:
                    # If the error was raised after a DB exception, the session may be
                    # in an aborted transaction; rollback first so we can persist status.
                    await session.rollback()
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)
                    await session.commit()

                    # Send error details through SSE so frontend can show helpful message
                    publisher.publish_error(str(e), e.error_code)
                    publisher.publish_finalize("error")
                except Exception:
                    logger.error("Failed to update job status after error", exc_info=True)

                # Return failure status - do NOT raise (no retry)
                return {
                    "status": "failed",
                    "job_id": job_id,
                    "error": str(e),
                    "error_code": e.error_code,
                    "retryable": False,
                }

            except Exception as e:
                logger.error("Deal meeting job failed %s: %s", job_id, e, exc_info=True)
                # Try to update job status
                try:
                    # Ensure we can write status updates even if the original failure
                    # aborted the current transaction.
                    await session.rollback()
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
    except NonRetryableJobError:
        # NonRetryableJobError is handled inside _run() and returns a dict
        # This shouldn't happen, but if it does, don't retry
        logger.warning("NonRetryableJobError escaped _run() for job %s", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "retryable": False,
        }
    except Exception as e:
        logger.error("Deal meeting job task failed %s: %s", job_id, e, exc_info=True)
        # Exponential backoff: 60, 120, 240 seconds
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
