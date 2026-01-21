"""Celery task for meeting summary generation.

This task processes MEETING_SUMMARY jobs:
- Post-meeting summary generation (requires transcript)

Both manual (API-triggered) and automated (pipeline-triggered) jobs use the same
task via DealJobs records. See DealMeetingSummaryPipelineService for automated flow.

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
    MeetingSummaryService,
    NonRetryableJobError,
)

logger = get_logger(__name__)

# Sentinel user ID for automated pipeline jobs
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


@app.task(bind=True, name="meeting_summary_task.process", acks_late=True, max_retries=3)
def process_meeting_summary(self: Task, organisation_id: str, job_id: str):
    """Process a meeting summary job.

    Follows fresh session pattern:
    - Phase 1: Load job, verify type, mark IN_PROGRESS (fresh session)
    - Phase 2: Call MeetingSummaryService (service manages its own sessions)
    - Phase 3: Save result (fresh session)

    Args:
        organisation_id: The organisation ID (as string for JSON serialization)
        job_id: The job ID to process (as string for JSON serialization)

    Returns:
        Dictionary with job completion status
    """
    logger.info("Processing meeting summary job org=%s job=%s", organisation_id, job_id)

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Phase 1: Load job context (fresh session)
        # Initialize variables that will be set in the session block
        deal_id: UUID
        event_id: UUID
        job_type_str: str
        user_id: UUID
        job_metadata: dict

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            # Verify job type
            if job.job_type != DealJobType.MEETING_SUMMARY:
                raise RuntimeError(f"Invalid job type {job.job_type} for meeting summary task")

            # Extract values before closing session
            if not job.deal_id or not job.event_id:
                raise NonRetryableJobError("Meeting summary requires event_id", "invalid_data")

            deal_id = job.deal_id
            event_id = job.event_id
            job_type_str = str(job.job_type)
            user_id = job.user_id
            job_metadata = job.job_metadata or {}

            # Mark as IN_PROGRESS
            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        # Determine if automated (for logging)
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )

        logger.info(
            "Loaded meeting summary job: job=%s deal=%s event=%s automated=%s",
            job_id,
            deal_id,
            event_id,
            is_automated,
        )

        # Create publisher for SSE updates
        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str)

        # Phase 2: Call service (service manages its own sessions)
        try:
            logger.info("Generating meeting summary for job %s", job_id)
            service = MeetingSummaryService(
                organisation_id=org_uuid,
                deal_id=deal_id,
                event_id=event_id,
                publisher=publisher,
            )
            result = await service.generate()

        except NonRetryableJobError as e:
            logger.info(
                "Meeting summary job permanently failed %s: %s (code=%s)",
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
            logger.error("Meeting summary job failed %s: %s", job_id, e, exc_info=True)

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

        logger.info("Meeting summary job completed: %s", job_id)
        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": str(DealJobType.MEETING_SUMMARY),
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
        logger.error("Meeting summary task failed %s: %s", job_id, e, exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
