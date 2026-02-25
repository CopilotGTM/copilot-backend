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

IMPORT_TASK_SOFT_TIME_LIMIT_SECONDS = 1200  # 20 minutes soft limit
IMPORT_TASK_TIME_LIMIT_SECONDS = 1260  # 21 minutes hard limit


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce persisted metadata values into strict booleans."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _mark_job_failed(organisation_id: UUID, job_id: UUID, error_message: str) -> None:
    async def _run_mark_failed():
        async for session in acquire_org_async_session(organisation_id):
            job = await session.get(DealJobs, job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error_message = error_message
                await session.commit()

    run_async(_run_mark_failed())


@app.task(
    bind=True,
    name="deal_import_task.process",
    acks_late=True,
    max_retries=2,
    soft_time_limit=IMPORT_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=IMPORT_TASK_TIME_LIMIT_SECONDS,
)
def process_deal_import_job(self: Task, organisation_id: str, job_id: str):
    logger.info("Processing deal import job org=%s job=%s", organisation_id, job_id)

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Session is released after this block — we extract metadata into local vars
        # so the DB connection returns to the pool before the long-running import starts
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal import job {job_id} not found")
            job.status = JobStatus.IN_PROGRESS
            await session.commit()
            job_metadata = job.job_metadata or {}
            storage_path = job_metadata.get("storage_path")
            actor_user_id = job.user_id
            import_mode = str(job_metadata.get("import_mode") or "upsert").strip().lower()
            emit_row_events = _coerce_bool(job_metadata.get("emit_row_events"), True)
            strict_validation = _coerce_bool(job_metadata.get("strict_validation"), False)
            raw_pipeline_id = job_metadata.get("pipeline_id")
            pipeline_id = UUID(raw_pipeline_id) if raw_pipeline_id else None

        if not storage_path:
            raise DealWriteServiceError("Deal import job missing storage_path", status_code=400)

        publisher = DealJobRedisPublisher(org_uuid, job_uuid, DealJobType.DEAL_IMPORT.value)
        service = DealImportProcessingService(
            organisation_id=org_uuid,
            actor_user_id=actor_user_id,
            storage_path=storage_path,
            publisher=publisher,
            import_mode=import_mode,
            emit_row_events=emit_row_events,
            strict_validation=strict_validation,
            pipeline_id=pipeline_id,
        )

        result_data = await service.process()

        # Persist result to DB so clients that reconnect after completion can still get it
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
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)
        publisher = DealJobRedisPublisher(org_uuid, job_uuid, DealJobType.DEAL_IMPORT.value)
        _mark_job_failed(org_uuid, job_uuid, error_message)
        publisher.publish_error(error_message, "import_failed")
        publisher.publish_finalize("error")
        return {"status": "failed", "job_id": job_id, "error": error_message}
    except Exception as exc:
        logger.error("Deal import task failed %s: %s", job_id, exc, exc_info=True)
        retries = getattr(self.request, "retries", 0)
        max_retries = getattr(self, "max_retries", 0) or 0
        if retries >= max_retries:
            error_message = f"Deal import task failed after retries: {exc}"
            logger.error("Deal import job reached max retries %s: %s", job_id, error_message)
            org_uuid = UUID(organisation_id)
            job_uuid = UUID(job_id)
            _mark_job_failed(org_uuid, job_uuid, error_message)
            publisher = DealJobRedisPublisher(org_uuid, job_uuid, DealJobType.DEAL_IMPORT.value)
            publisher.publish_error(error_message, "import_failed")
            publisher.publish_finalize("error")
            return {"status": "failed", "job_id": job_id, "error": error_message}

        # Exponential backoff
        raise self.retry(exc=exc, countdown=60 * (2**retries)) from exc
