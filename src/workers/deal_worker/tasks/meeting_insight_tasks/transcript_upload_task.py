"""Celery task for processing manually uploaded transcript files.

This task processes TRANSCRIPT_UPLOAD jobs:
- Extracts text from uploaded PDF/Word files
- Saves transcript as a Meeting record
- Triggers meeting summary generation

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
from src.workers.deal_worker.services.meeting_insight_services.transcript_upload_service import (
    TranscriptUploadError,
    TranscriptUploadService,
)

logger = get_logger(__name__)


@app.task(bind=True, name="transcript_upload_task.process", acks_late=True, max_retries=2)
def process_transcript_upload(self: Task, organisation_id: str, job_id: str):
    """Process an uploaded transcript file.

    Args:
        organisation_id: The organisation ID (as string for JSON serialization)
        job_id: The job ID to process (as string for JSON serialization)
    """
    logger.info(
        "Processing transcript upload job",
        extra={
            "event": "transcript_upload_job_processing",
            "organisation_id": str(organisation_id),
            "job_id": str(job_id),
        },
    )

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Phase 1: Load job context (fresh session)
        deal_id: UUID
        event_id: UUID
        user_id: UUID
        job_type_str: str
        storage_path: str
        mimetype: str

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")

            if job.job_type != DealJobType.TRANSCRIPT_UPLOAD:
                raise RuntimeError(f"Invalid job type {job.job_type} for transcript upload task")

            if not job.deal_id or not job.event_id:
                raise TranscriptUploadError(
                    "Transcript upload requires deal_id and event_id", "invalid_data"
                )

            deal_id = job.deal_id
            event_id = job.event_id
            user_id = job.user_id
            job_type_str = str(job.job_type)
            job_metadata = job.job_metadata or {}
            storage_path = job_metadata["storage_path"]
            mimetype = job_metadata["mimetype"]

            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        logger.info(
            "Loaded transcript upload job",
            extra={
                "event": "transcript_upload_job_loaded",
                "organisation_id": str(org_uuid),
                "job_id": str(job_uuid),
                "deal_id": str(deal_id),
                "event_id": str(event_id),
            },
        )

        publisher = DealJobRedisPublisher(org_uuid, job_uuid, job_type_str)

        # Phase 2: Call service (service manages its own sessions)
        try:
            service = TranscriptUploadService(
                organisation_id=org_uuid,
                deal_id=deal_id,
                event_id=event_id,
                user_id=user_id,
                publisher=publisher,
            )
            result = await service.process(storage_path, mimetype)

        except TranscriptUploadError as e:
            logger.info(
                "Transcript upload job permanently failed",
                extra={
                    "event": "transcript_upload_job_failed_permanently",
                    "organisation_id": str(org_uuid),
                    "job_id": str(job_uuid),
                    "deal_id": str(deal_id),
                    "event_id": str(event_id),
                    "error_code": e.error_code,
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
                "Transcript upload job failed",
                extra={
                    "event": "transcript_upload_job_failed",
                    "organisation_id": str(org_uuid),
                    "job_id": str(job_uuid),
                    "deal_id": str(deal_id),
                    "event_id": str(event_id),
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

        # Phase 3: Save result (fresh session)
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = result
                await session.commit()

        publisher.publish_result(result)
        publisher.publish_finalize("done")

        logger.info(
            "Transcript upload job completed",
            extra={
                "event": "transcript_upload_job_completed",
                "organisation_id": str(org_uuid),
                "job_id": str(job_uuid),
                "deal_id": str(deal_id),
                "event_id": str(event_id),
            },
        )
        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": str(DealJobType.TRANSCRIPT_UPLOAD),
        }

    try:
        return run_async(_run())
    except TranscriptUploadError:
        logger.warning(
            "TranscriptUploadError escaped transcript upload task",
            extra={
                "event": "transcript_upload_task_nonretryable_escape",
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
            "Transcript upload task failed",
            extra={
                "event": "transcript_upload_task_failed",
                "organisation_id": str(organisation_id),
                "job_id": str(job_id),
            },
        )
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
