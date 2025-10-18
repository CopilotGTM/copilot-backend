from __future__ import annotations

from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.infra.utils.logger import get_logger

logger = get_logger("knowledge_job_utils")


def mark_job_in_progress(session, job: KnowledgeProcessingJob, source_id: str) -> None:
    """Set job to IN_PROGRESS with commit and basic rollback safety."""
    try:
        logger.info("Marking job IN_PROGRESS for source %s", source_id)
        job.status = JobStatus.IN_PROGRESS
        session.add(job)
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("Failed to mark job IN_PROGRESS for source %s", source_id)


def set_job_status(job: KnowledgeProcessingJob | None, status: JobStatus | None) -> None:
    """Set a job status without committing. Safe to call with None job/status."""
    if job is None or status is None:
        return
    job.status = status
