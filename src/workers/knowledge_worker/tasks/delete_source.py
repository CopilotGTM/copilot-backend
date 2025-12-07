from __future__ import annotations

"""Deletion task for knowledge sources and related data.

Performs idempotent cleanup of:
- Derived documents in `knowledge_base` (by source_id)
- Processing jobs for the source (except the delete job itself)
- Original and thumbnail blobs in storage (if present)
- OrganisationSource record itself

Enhanced with:
- Job tracking via KnowledgeProcessingJob
- SSE status updates (0% start, 100% complete)
"""

import asyncio
import contextlib
from datetime import UTC, datetime

from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate
from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.domain.models.organisation_tables.knowledge_models import OrganisationSource
from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.utils import DocumentStore

logger = get_logger("knowledge_delete_task")

# Job type constant for deletion tracking
DELETE_SOURCE_JOB_TYPE = "delete_source"


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


def _get_or_create_delete_job(
    session, source_id: str, job_id: str | None
) -> KnowledgeProcessingJob | None:
    """Get existing delete job or create one if not provided."""
    if job_id:
        job = session.get(KnowledgeProcessingJob, job_id)
        if job:
            return job

    # Try to find existing delete job for this source
    existing = (
        session.query(KnowledgeProcessingJob)
        .filter(
            KnowledgeProcessingJob.source_id == source_id,
            KnowledgeProcessingJob.job_type == DELETE_SOURCE_JOB_TYPE,
        )
        .first()
    )
    return existing


def _update_job_status(
    session,
    job: KnowledgeProcessingJob | None,
    status: JobStatus,
    error_message: str | None = None,
) -> None:
    """Update job status with commit."""
    if not job:
        return
    try:
        job.status = status
        if error_message:
            job.error_message = error_message
        session.add(job)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.warning("Failed to update job status: %s", e)


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

    # DB session & storage
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Track job for status updates
    job = _get_or_create_delete_job(session, source_id, job_id)

    try:
        # Mark job as in-progress
        _update_job_status(session, job, JobStatus.IN_PROGRESS)

        # Publish initial SSE: deletion started
        _publish_deletion_status(organisation_id, source_id, "deleting", progress=0)

        # Fetch source (may be missing if already deleted)
        src = session.get(OrganisationSource, source_id)

        # Step 1: Delete derived documents
        try:
            ds = DocumentStore(organisation_id)
            deleted_docs = ds.delete_documents_by_source(source_id)
            logger.info("Deleted %s derived documents for source %s", deleted_docs, source_id)
        except Exception as e:
            logger.warning("Failed deleting documents for %s: %s", source_id, e)

        # Step 2: Delete processing jobs except the delete job itself
        try:
            delete_query = session.query(KnowledgeProcessingJob).filter(
                KnowledgeProcessingJob.source_id == source_id,
                KnowledgeProcessingJob.job_type != DELETE_SOURCE_JOB_TYPE,
            )
            deleted_jobs = delete_query.delete()
            session.commit()
            logger.info("Deleted %s processing jobs for source %s", deleted_jobs, source_id)
        except Exception as e:
            session.rollback()
            logger.warning("Failed deleting jobs for %s: %s", source_id, e)

        # Step 3: Delete blobs (original + thumbnails)
        try:
            if src and src.storage_path:
                # original
                loop.run_until_complete(storage.delete_blob(src.storage_path))

                # thumbnails
                if getattr(src, "thumbnail_small_path", None):
                    loop.run_until_complete(storage.delete_blob(src.thumbnail_small_path))
                if getattr(src, "thumbnail_medium_path", None):
                    loop.run_until_complete(storage.delete_blob(src.thumbnail_medium_path))
        except Exception as e:
            logger.warning("Failed deleting blobs for %s: %s", source_id, e)

        # Step 4: Delete the source record and the delete job
        try:
            # Delete the delete job first
            if job:
                session.delete(job)

            # Then delete the source
            if src:
                session.delete(src)

            session.commit()
            logger.info("Deleted OrganisationSource %s", source_id)

            # Publish final SSE: deletion completed
            _publish_deletion_status(organisation_id, source_id, "deleted", progress=100)

        except Exception as e:
            session.rollback()
            logger.warning("Failed deleting OrganisationSource %s: %s", source_id, e)
            # Still mark as failed but don't re-raise - partial cleanup is ok
            _publish_deletion_status(
                organisation_id,
                source_id,
                "failed",
                error_message=f"Failed to delete source record: {e}",
            )

    except Exception as e:
        # Unexpected error - mark job as failed and publish error
        logger.error("Deletion task failed for source %s: %s", source_id, e, exc_info=True)
        _update_job_status(session, job, JobStatus.FAILED, str(e))
        _publish_deletion_status(
            organisation_id,
            source_id,
            "failed",
            error_message=f"Deletion failed: {e}",
        )
        raise self.retry(exc=e, countdown=60) from e  # Retry after 1 minute

    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
        # Close async Azure clients (run in event loop)
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
