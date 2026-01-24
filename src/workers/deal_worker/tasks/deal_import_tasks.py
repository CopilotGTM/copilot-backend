"""Celery task for processing bulk deal imports."""

from __future__ import annotations

from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.internal.deal_write_service import DealWriteServiceError
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_import_service import DealImportProcessingService
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


@app.task(bind=True, name="deal_import_task.process", acks_late=True, max_retries=2)
def process_deal_import_job(self: Task, organisation_id: str, job_id: str):
    logger.info("Processing deal import job org=%s job=%s", organisation_id, job_id)

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Load job metadata and mark in progress
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal import job {job_id} not found")
            job.status = JobStatus.IN_PROGRESS
            await session.commit()
            job_metadata = job.job_metadata or {}
            storage_path = job_metadata.get("storage_path")
            actor_user_id = job.user_id

        if not storage_path:
            raise DealWriteServiceError("Deal import job missing storage_path", status_code=400)

        publisher = DealJobRedisPublisher(org_uuid, job_uuid, DealJobType.DEAL_IMPORT.value)
        service = DealImportProcessingService(
            organisation_id=org_uuid,
            actor_user_id=actor_user_id,
            storage_path=storage_path,
            publisher=publisher,
        )

        result_data = await service.process()

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = result_data
                await session.commit()

        publisher.publish_result(result_data)
        publisher.publish_finalize("done")
        logger.info(
            "Deal import job completed org=%s job=%s created=%s failed=%s",
            organisation_id,
            job_id,
            result_data.get("created_count", 0),
            result_data.get("failed_count", 0),
        )
        return {"status": "completed", "job_id": job_id}

    try:
        return run_async(_run())
    except DealWriteServiceError as exc:
        error_message = exc.message
        logger.info("Deal import job failed %s: %s", job_id, error_message)
        publisher = DealJobRedisPublisher(
            UUID(organisation_id), UUID(job_id), DealJobType.DEAL_IMPORT.value
        )

        async def _mark_failed():
            async for session in acquire_org_async_session(UUID(organisation_id)):
                job = await session.get(DealJobs, UUID(job_id))
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = error_message
                    await session.commit()

        run_async(_mark_failed())
        publisher.publish_error(error_message, "import_failed")
        publisher.publish_finalize("error")
        return {"status": "failed", "job_id": job_id, "error": error_message}
    except Exception as exc:
        logger.error("Deal import task failed %s: %s", job_id, exc, exc_info=True)
        # Exponential backoff
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
