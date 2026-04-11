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
from src.workers.deal_worker.utils.pipeline_dispatch import (
    dispatch_deal_insight_pipeline_async,
    get_or_create_deal_memory_job,
)

logger = get_logger(__name__)

# Sentinel user ID for automated pipeline jobs
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


async def _dispatch_insight_pipeline_for_manual_summary(
    organisation_id: UUID, deal_id: UUID
) -> None:
    """Dispatch the deal insight pipeline after a manual meeting summary completes.

    Creates (or reuses) a DEAL_MEMORY_BUILD job and fires the full insight chain:
    deal_memory -> group(risk_blocker, stakeholder_analysis).
    """
    try:
        deal_memory_job_id: UUID | None = None
        async for session in acquire_org_async_session(organisation_id):
            deal_memory_job_id, _ = await get_or_create_deal_memory_job(
                session,
                organisation_id,
                deal_id,
                source="transcript_upload",
                trigger="meeting_summary_completed",
            )
            await session.commit()

        if deal_memory_job_id:
            await dispatch_deal_insight_pipeline_async(
                str(organisation_id),
                str(deal_id),
                str(deal_memory_job_id),
            )
            logger.info(
                "Dispatched insight pipeline after manual summary",
                extra={
                    "event": "meeting_summary_insight_pipeline_dispatched",
                    "organisation_id": str(organisation_id),
                    "deal_id": str(deal_id),
                    "job_id": str(deal_memory_job_id),
                },
            )
    except Exception:
        logger.exception(
            "Failed to dispatch insight pipeline after manual summary",
            extra={
                "event": "meeting_summary_insight_pipeline_dispatch_failed",
                "organisation_id": str(organisation_id),
                "deal_id": str(deal_id),
            },
        )


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
    logger.info(
        "Processing meeting summary job",
        extra={
            "event": "meeting_summary_job_processing",
            "organisation_id": str(organisation_id),
            "job_id": str(job_id),
        },
    )

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
            "Loaded meeting summary job",
            extra={
                "event": "meeting_summary_job_loaded",
                "organisation_id": str(org_uuid),
                "job_id": str(job_uuid),
                "deal_id": str(deal_id),
                "event_id": str(event_id),
                "automated": is_automated,
            },
        )

        # Create publisher for SSE updates
        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str)

        # Phase 2: Call service (service manages its own sessions)
        service = MeetingSummaryService(
            organisation_id=org_uuid,
            deal_id=deal_id,
            event_id=event_id,
            publisher=publisher,
        )
        try:
            logger.info(
                "Generating meeting summary",
                extra={
                    "event": "meeting_summary_generating",
                    "organisation_id": str(org_uuid),
                    "job_id": str(job_uuid),
                    "deal_id": str(deal_id),
                    "event_id": str(event_id),
                    "automated": is_automated,
                },
            )
            result = await service.generate()

        except NonRetryableJobError as e:
            logger.info(
                "Meeting summary job permanently failed",
                extra={
                    "event": "meeting_summary_job_failed_permanently",
                    "organisation_id": str(org_uuid),
                    "job_id": str(job_uuid),
                    "deal_id": str(deal_id),
                    "event_id": str(event_id),
                    "error_code": e.error_code,
                    "automated": is_automated,
                },
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
            logger.exception(
                "Meeting summary job failed",
                extra={
                    "event": "meeting_summary_job_failed",
                    "organisation_id": str(org_uuid),
                    "job_id": str(job_uuid),
                    "deal_id": str(deal_id),
                    "event_id": str(event_id),
                    "automated": is_automated,
                },
            )

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

        logger.info(
            "Meeting summary job completed",
            extra={
                "event": "meeting_summary_job_completed",
                "organisation_id": str(org_uuid),
                "job_id": str(job_uuid),
                "deal_id": str(deal_id),
                "event_id": str(event_id),
                "automated": is_automated,
            },
        )

        # Trigger insight pipeline for non-automated jobs (e.g. manual transcript uploads).
        # Automated pipeline jobs already have the insight chain wired by process_deal_pipeline.
        skip_insight_pipeline = bool(job_metadata.get("skip_insight_pipeline"))
        if not is_automated and not skip_insight_pipeline:
            await _dispatch_insight_pipeline_for_manual_summary(org_uuid, deal_id)

        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": str(DealJobType.MEETING_SUMMARY),
        }

    try:
        return run_async(_run())
    except NonRetryableJobError:
        logger.warning(
            "NonRetryableJobError escaped meeting summary task",
            extra={
                "event": "meeting_summary_task_nonretryable_escape",
                "organisation_id": str(organisation_id),
                "job_id": str(job_id),
            },
        )
        return {
            "status": "failed",
            "job_id": job_id,
            "retryable": False,
        }
    except Exception as e:
        logger.exception(
            "Meeting summary task failed",
            extra={
                "event": "meeting_summary_task_failed",
                "organisation_id": str(organisation_id),
                "job_id": str(job_id),
            },
        )
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
