"""Celery task for meeting prep (briefing) generation.

This task processes MEETING_BRIEFING jobs:
- Pre-meeting preparation briefing with AI-generated insights

Both manual (API-triggered) and automated jobs use the same
task via DealJobs records.

Task follows fresh session pattern:
- Phase 1: Load job context (fresh session)
- Phase 2: Call service (service manages its own sessions)
- Phase 3: Save result (fresh session)
"""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.services.meeting_insight_services import (
    MeetingPrepService,
    NonRetryableJobError,
)

logger = get_logger(__name__)

# Sentinel user ID for automated pipeline jobs
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


@app.task(bind=True, name="meeting_prep_task.process", acks_late=True, max_retries=3)
def process_meeting_prep(self: Task, organisation_id: str, job_id: str):
    """Process a meeting prep (briefing) job.

    Follows fresh session pattern:
    - Phase 1: Load job, verify type, mark IN_PROGRESS (fresh session)
    - Phase 2: Call MeetingPrepService (service manages its own sessions)
    - Phase 3: Save result (fresh session)

    Args:
        organisation_id: The organisation ID (as string for JSON serialization)
        job_id: The job ID to process (as string for JSON serialization)

    Returns:
        Dictionary with job completion status
    """
    logger.info("Processing meeting prep job org=%s job=%s", organisation_id, job_id)

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Phase 1: Load job context (fresh session)
        deal_id: UUID | None = None
        event_id: UUID | None = None
        job_type_str: str = ""

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            deal_id = job.deal_id
            event_id = job.event_id
            job_type_str = str(job.job_type)

            if not deal_id or not event_id:
                raise NonRetryableJobError("Meeting prep requires event_id", "invalid_data")

            # Mark as IN_PROGRESS
            job.status = JobStatus.IN_PROGRESS
            await session.commit()

            # Determine if automated (for logging)
            is_automated = (
                job.user_id == AUTOMATED_PIPELINE_USER_ID
                or job.job_metadata.get("source") == "automated_pipeline"
            )

            logger.info(
                "Loaded meeting prep job: job=%s deal=%s event=%s automated=%s",
                job_id,
                deal_id,
                event_id,
                is_automated,
            )
        # Session released

        # Create publisher for SSE updates
        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str)

        # Phase 2: Call service (service manages its own sessions)
        try:
            logger.info("Generating meeting prep for job %s", job_id)
            service = MeetingPrepService(
                organisation_id=org_uuid,
                deal_id=deal_id,
                event_id=event_id,
                publisher=publisher,
            )
            result = await service.generate()

        except NonRetryableJobError as e:
            logger.info(
                "Meeting prep job permanently failed %s: %s (code=%s)",
                job_id,
                str(e),
                e.error_code,
            )

            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)
                    await session.commit()

            publisher.publish_error(str(e), e.error_code)
            publisher.publish_finalize("error")

            return {
                "status": "failed",
                "job_id": job_id,
                "error": str(e),
                "error_code": e.error_code,
                "retryable": False,
            }

        except Exception as e:
            logger.error("Meeting prep job failed %s: %s", job_id, e, exc_info=True)

            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = str(e)[:500]
                    job.retry_count += 1
                    await session.commit()

            publisher.publish_finalize("error")
            raise

        # Phase 3: Save result (fresh session)
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = result
                await session.commit()

        publisher.publish_result(result)
        publisher.publish_finalize("done")

        logger.info("Meeting prep job completed: %s", job_id)
        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": str(DealJobType.MEETING_BRIEFING),
        }

    try:
        return run_async(_run())
    except NonRetryableJobError:
        logger.warning("NonRetryableJobError escaped _run() for job %s", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "retryable": False,
        }
    except Exception as e:
        logger.error("Meeting prep task failed %s: %s", job_id, e, exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
