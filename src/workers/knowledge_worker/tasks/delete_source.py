"""Deletion task for knowledge sources and related data.

Performs idempotent cleanup of:
- Derived documents in `knowledge_base` (by source_id)
- Processing jobs for the source (except the delete job itself)
- Original and thumbnail blobs in storage (if present)
- OrganisationSource record itself

Enhanced with:
- Job tracking via KnowledgeProcessingJob
- SSE status updates (0% start, 100% complete)

IMPORTANT: This task follows the fresh session pattern to avoid holding
database connections during Azure blob operations. Sessions are acquired
only when needed for DB operations and released before any slow external calls.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import col

from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.domain.models.organisation_tables.knowledge_models import OrganisationSource
from src.infra.database.sessions import org_session_scope
from src.infra.messaging.event_models import KnowledgeSourceEventUpdate
from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.utils import DocumentStore

logger = get_logger("knowledge_delete_task")

# Job type constant for deletion tracking
DELETE_SOURCE_JOB_TYPE = "delete_source"


@dataclass
class SourceBlobData:
    """Data extracted from source record for blob deletion without holding session."""

    storage_path: str | None
    thumbnail_small_path: str | None
    thumbnail_medium_path: str | None


def _publish_deletion_status(
    organisation_id: str,
    source_id: str,
    status: str,
    progress: int | None = None,
    error_message: str | None = None,
) -> None:
    """Publish deletion status update for SSE consumers."""
    try:
        event = KnowledgeSourceEventUpdate(
            source_id=source_id,
            status=status,
            updated_at=datetime.now(UTC).isoformat(),
            error_message=error_message,
            progress=progress,
        )
        publish_source_status(organisation_id, event)
    except Exception as e:
        logger.warning("Failed to publish deletion status for %s: %s", source_id, e)


def _as_uuid(value: str | UUID) -> UUID:
    """Normalize UUID inputs (string or UUID) to UUID type."""
    return value if isinstance(value, UUID) else UUID(value)


def _load_source_and_mark_in_progress(
    organisation_id: str | UUID,
    source_id: str | UUID,
    job_id: str | UUID | None,
) -> tuple[SourceBlobData | None, str | None]:
    """
    Load source data and mark job in-progress.

    Uses fresh session pattern: acquires session, extracts needed data, releases session.

    Returns:
        Tuple of (SourceBlobData or None if not found, job_id string or None)
    """
    org_uuid = _as_uuid(organisation_id)
    source_uuid = _as_uuid(source_id)
    job_uuid = _as_uuid(job_id) if job_id else None

    with org_session_scope(org_uuid) as session:
        # Find or get the delete job
        job: KnowledgeProcessingJob | None = None
        if job_uuid:
            job = session.get(KnowledgeProcessingJob, job_uuid)

        if not job:
            # Try to find existing delete job for this source
            job = (
                session.query(KnowledgeProcessingJob)
                .filter(
                    col(KnowledgeProcessingJob.source_id) == source_uuid,
                    col(KnowledgeProcessingJob.job_type) == DELETE_SOURCE_JOB_TYPE,
                )
                .first()
            )

        # Mark job as in-progress
        if job:
            job.status = JobStatus.IN_PROGRESS
            session.add(job)
            session.commit()

        # Fetch source (may be missing if already deleted)
        src = session.get(OrganisationSource, source_uuid)

        if not src:
            return None, str(job.job_id) if job else None

        # Extract blob paths before releasing session
        blob_data = SourceBlobData(
            storage_path=src.storage_path,
            thumbnail_small_path=getattr(src, "thumbnail_small_path", None),
            thumbnail_medium_path=getattr(src, "thumbnail_medium_path", None),
        )

        return blob_data, str(job.job_id) if job else None


def _delete_processing_jobs(organisation_id: str | UUID, source_id: str | UUID) -> int:
    """
    Delete processing jobs for the source (except delete job).

    Uses fresh session pattern.

    Returns:
        Number of jobs deleted
    """
    org_uuid = _as_uuid(organisation_id)
    source_uuid = _as_uuid(source_id)

    with org_session_scope(org_uuid) as session:
        try:
            delete_query = session.query(KnowledgeProcessingJob).filter(
                col(KnowledgeProcessingJob.source_id) == source_uuid,
                col(KnowledgeProcessingJob.job_type) != DELETE_SOURCE_JOB_TYPE,
            )
            deleted_jobs = delete_query.delete()
            session.commit()
            return deleted_jobs
        except Exception as e:
            session.rollback()
            logger.warning("Failed deleting jobs for %s: %s", source_id, e)
            return 0


def _delete_blobs(
    storage: AzureOrganisationBlobStorage,
    blob_data: SourceBlobData,
    source_id: str,
) -> None:
    """
    Delete blobs from Azure storage.

    This is a pure Azure operation - no database session needed.
    """
    try:
        if blob_data.storage_path:
            run_async(storage.delete_blob(blob_data.storage_path))

        if blob_data.thumbnail_small_path:
            run_async(storage.delete_blob(blob_data.thumbnail_small_path))

        if blob_data.thumbnail_medium_path:
            run_async(storage.delete_blob(blob_data.thumbnail_medium_path))

    except Exception as e:
        logger.warning("Failed deleting blobs for %s: %s", source_id, e)


def _delete_source_and_job(
    organisation_id: str | UUID,
    source_id: str | UUID,
    job_id: str | UUID | None,
) -> bool:
    """
    Delete the source record and delete job.

    Uses fresh session pattern.

    Returns:
        True if successful, False otherwise
    """
    org_uuid = _as_uuid(organisation_id)
    source_uuid = _as_uuid(source_id)
    job_uuid = _as_uuid(job_id) if job_id else None

    with org_session_scope(org_uuid) as session:
        try:
            # Delete the delete job first
            if job_uuid:
                job = session.get(KnowledgeProcessingJob, job_uuid)
                if job:
                    session.delete(job)

            # Then delete the source
            src = session.get(OrganisationSource, source_uuid)
            if src:
                session.delete(src)

            session.commit()
            logger.info("Deleted OrganisationSource %s", source_id)
            return True

        except Exception as e:
            session.rollback()
            logger.warning("Failed deleting OrganisationSource %s: %s", source_id, e)
            return False


def _mark_job_failed(
    organisation_id: str | UUID,
    job_id: str | UUID | None,
    error_message: str,
) -> None:
    """Mark the delete job as failed with fresh session."""
    if not job_id:
        return

    org_uuid = _as_uuid(organisation_id)
    job_uuid = _as_uuid(job_id)

    with org_session_scope(org_uuid) as session:
        try:
            job = session.get(KnowledgeProcessingJob, job_uuid)
            if job:
                job.status = JobStatus.FAILED
                job.error_message = error_message
                session.add(job)
                session.commit()
        except Exception as e:
            session.rollback()
            logger.warning("Failed to mark job as failed: %s", e)


@app.task(
    name="knowledge.delete_source",
    bind=True,
    acks_late=True,
    max_retries=3,
)
def delete_source_task(
    self, organisation_id: str, source_id: str, job_id: str | None = None
):  # pragma: no cover
    """Delete a knowledge source and all related resources with job tracking.

    This task follows the fresh session pattern:
    1. Load source data and mark job in-progress (fresh session, released after)
    2. Delete derived documents (DocumentStore manages its own session)
    3. Delete processing jobs (fresh session, released after)
    4. Delete blobs (NO session - Azure operations only)
    5. Delete source record and job (fresh session, released after)

    Args:
        organisation_id: The organisation UUID
        source_id: The source UUID to delete
        job_id: Optional job UUID for tracking (created by API layer)

    Safety measures:
    - All delete operations are best-effort and idempotent.
    - Missing records or blobs are logged and ignored.
    - SSE updates published at each stage for real-time progress.
    """
    logger.info(
        "Deleting knowledge source %s in org %s (job_id=%s)",
        source_id,
        organisation_id,
        job_id,
    )

    org_uuid = _as_uuid(organisation_id)
    storage = AzureOrganisationBlobStorage(organisation_id=org_uuid)
    tracked_job_id: str | None = None

    try:
        # Phase 1: Load source data and mark job in-progress (fresh session)
        blob_data, tracked_job_id = _load_source_and_mark_in_progress(
            organisation_id, source_id, job_id
        )

        # Publish initial SSE: deletion started
        _publish_deletion_status(organisation_id, source_id, "deleting", progress=0)

        # Phase 2: Delete derived documents (DocumentStore manages its own session)
        try:
            ds = DocumentStore(organisation_id)
            deleted_docs = ds.delete_documents_by_source(source_id)
            logger.info("Deleted %s derived documents for source %s", deleted_docs, source_id)
        except Exception as e:
            logger.warning("Failed deleting documents for %s: %s", source_id, e)

        # Phase 3: Delete processing jobs (fresh session)
        deleted_jobs = _delete_processing_jobs(organisation_id, source_id)
        logger.info("Deleted %s processing jobs for source %s", deleted_jobs, source_id)

        # Phase 4: Delete blobs (NO session - Azure operations only)
        if blob_data:
            _delete_blobs(storage, blob_data, source_id)

        # Phase 5: Delete source record and job (fresh session)
        success = _delete_source_and_job(organisation_id, source_id, tracked_job_id)

        if success:
            # Publish final SSE: deletion completed
            _publish_deletion_status(organisation_id, source_id, "deleted", progress=100)
        else:
            # Partial failure - source record deletion failed
            _publish_deletion_status(
                organisation_id,
                source_id,
                "failed",
                error_message="Failed to delete source record",
            )

    except Exception as e:
        # Unexpected error - mark job as failed and publish error
        logger.error("Deletion task failed for source %s: %s", source_id, e, exc_info=True)
        _mark_job_failed(organisation_id, tracked_job_id, str(e))
        _publish_deletion_status(
            organisation_id,
            source_id,
            "failed",
            error_message=f"Deletion failed: {e}",
        )
        raise self.retry(exc=e, countdown=60) from e  # Retry after 1 minute

    finally:
        # Close async Azure clients using persistent thread-local event loop
        with contextlib.suppress(Exception):
            run_async(storage.close())
