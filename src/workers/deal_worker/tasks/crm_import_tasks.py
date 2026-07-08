from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_master_async_session, org_async_session_scope
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.imports.import_service import CRMImportService
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)

__all__ = [
    "analyze_csv_import",
    "analyze_csv_import_task",
    "process_csv_import",
    "process_csv_import_task",
]


@app.task(
    bind=True,
    name="crm_import.analyze_csv",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=300,
    time_limit=360,
)
def analyze_csv_import_task(
    self: Task,
    organisation_id: str,
    import_job_id: str,
) -> dict[str, Any]:
    org_id = UUID(organisation_id)
    job_id = UUID(import_job_id)
    try:
        return run_async(analyze_csv_import(org_id, job_id))
    except Exception as exc:
        logger.error(
            "CRM CSV analyze task failed",
            exc_info=True,
            extra={"organisation_id": organisation_id, "import_job_id": import_job_id},
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "import_job_id": import_job_id,
            "error": exc.__class__.__name__,
        }


@app.task(
    bind=True,
    name="crm_import.process_csv",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,
    time_limit=660,
)
def process_csv_import_task(
    self: Task,
    organisation_id: str,
    import_job_id: str,
) -> dict[str, Any]:
    org_id = UUID(organisation_id)
    job_id = UUID(import_job_id)
    try:
        return run_async(process_csv_import(org_id, job_id))
    except Exception as exc:
        logger.error(
            "CRM CSV process task failed",
            exc_info=True,
            extra={"organisation_id": organisation_id, "import_job_id": import_job_id},
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "import_job_id": import_job_id,
            "error": exc.__class__.__name__,
        }


async def analyze_csv_import(organisation_id: UUID, import_job_id: UUID) -> dict[str, Any]:
    try:
        result = await _run_with_org_session(
            organisation_id,
            lambda org_session: CRMImportService(
                org_session,
                organisation_id,
                user_id=None,
            ).analyze(import_job_id),
        )
    except Exception as exc:
        await _record_import_failure(
            organisation_id=organisation_id,
            import_job_id=import_job_id,
            error=str(exc),
        )
        return {
            "status": "failed",
            "organisation_id": str(organisation_id),
            "import_job_id": str(import_job_id),
            "error": exc.__class__.__name__,
        }
    return {
        "status": result.job.job_metadata.get("crm_import_status"),
        "organisation_id": str(organisation_id),
        "import_job_id": str(import_job_id),
    }


async def process_csv_import(organisation_id: UUID, import_job_id: UUID) -> dict[str, Any]:
    try:
        result = await _run_with_org_and_master_session(
            organisation_id,
            lambda org_session, master_session: CRMImportService(
                org_session,
                organisation_id,
                user_id=None,
                master_session=master_session,
            ).process_next_batch(import_job_id),
        )
    except Exception as exc:
        await _record_import_failure(
            organisation_id=organisation_id,
            import_job_id=import_job_id,
            error=str(exc),
        )
        return {
            "status": "failed",
            "organisation_id": str(organisation_id),
            "import_job_id": str(import_job_id),
            "error": exc.__class__.__name__,
        }
    return {
        "status": result.job.job_metadata.get("crm_import_status"),
        "organisation_id": str(organisation_id),
        "import_job_id": str(import_job_id),
        "result_data": result.job.result_data or {},
    }


async def _run_with_org_session(organisation_id: UUID, operation):
    async with org_async_session_scope(organisation_id) as org_session:
        return await operation(org_session)


async def _run_with_org_and_master_session(organisation_id: UUID, operation):
    async with org_async_session_scope(organisation_id) as org_session:
        async for master_session in acquire_master_async_session():
            return await operation(org_session, master_session)
    raise RuntimeError("Unable to acquire sessions for CRM import")


async def _record_import_failure(
    *,
    organisation_id: UUID,
    import_job_id: UUID,
    error: str,
) -> None:
    async with org_async_session_scope(organisation_id) as org_session:
        job = await org_session.get(DealJobs, import_job_id)
        if job is None or job.organisation_id != organisation_id:
            return
        job.status = JobStatus.FAILED
        job.error_message = error[:500]
        metadata = dict(job.job_metadata or {})
        metadata["crm_import_status"] = "failed"
        metadata["failed_at"] = datetime.now(UTC).isoformat()
        job.job_metadata = metadata
        org_session.add(job)
        await org_session.commit()
