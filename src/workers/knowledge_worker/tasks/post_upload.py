from __future__ import annotations

import contextlib
from uuid import UUID

from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.database.sessions import acquire_org_session
from src.infra.media.thumbnail_generator import (
    generate_thumbnail_sas_urls,
    generate_thumbnails,
)
from src.infra.observability.context import correlation_headers
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.services.progress import ProgressPublisher

logger = get_logger("knowledge_post_upload_task")


def _assign_generated_thumbnails(source: OrganisationSource, created: dict[str, str]):
    if not created:
        return
    if "thumb_small" in created:
        source.thumbnail_small_path = created["thumb_small"]
    if "thumb_medium" in created:
        source.thumbnail_medium_path = created["thumb_medium"]


def _compute_and_persist_checksum(
    storage: AzureOrganisationBlobStorage,
    source: OrganisationSource,
    session,
):
    """Compute checksum early (if missing) and commit immediately.

    Uses a persistent thread-local event loop via run_async() to prevent
    memory leaks from creating new event loops per task.

    Doing this before thumbnail generation ensures we still retain a checksum
    even if later steps fail (mirrors chat worker approach)."""
    try:
        if source.storage_path and not source.checksum:
            checksum = run_async(storage.compute_checksum(source.storage_path))
            source.checksum = checksum
            session.merge(source)
            session.commit()
    except Exception:
        logger.exception("Failed to compute checksum for source %s", source.source_id)


# Removed legacy status publish wrapper; use ProgressPublisher directly.


def _handle_duplicate_if_any(
    session,
    source: OrganisationSource,
    job: KnowledgeProcessingJob,
    publisher: ProgressPublisher,
) -> bool:
    """Detect duplicate by checksum and handle status transitions.

    Returns True if a duplicate was found and handled (caller should stop).
    """
    try:
        if not source.checksum:
            return False

        # Exclude soft-deleted and FAILED sources from duplicate consideration
        duplicate = (
            session.query(OrganisationSource)
            .filter(
                OrganisationSource.checksum == source.checksum,
                OrganisationSource.source_id != source.source_id,
                OrganisationSource.is_deleted.is_(False),
                OrganisationSource.status != OrganisationSourceStatus.FAILED,
            )
            .first()
        )
        if not duplicate:
            return False

        source.status = OrganisationSourceStatus.FAILED
        source.error_message = (
            f"Duplicate file detected; same content as source {duplicate.source_id}"
        )
        session.merge(source)

        job.status = JobStatus.COMPLETED
        session.add(job)
        session.commit()

        # Publish failure with a clear duplicate message for consumers (SSE/UI)
        publisher.publish(
            status=OrganisationSourceStatus.FAILED.value,
            error_message="file already exists",
        )

        logger.info(
            "Duplicate checksum found for source=%s; marked FAILED and job COMPLETED",
            source.source_id,
        )
        return True
    except Exception:
        logger.exception(
            "Error during duplicate checksum detection for source %s",
            source.source_id,
        )
        return False


@app.task(name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3)
def post_upload_process(self, organisation_id: UUID, source_id: UUID):
    """Post-upload processing: checksum and thumbnail generation.

    Uses a persistent thread-local event loop via run_async() to prevent
    memory leaks from creating new event loops per task.
    """
    logger.info("post_upload_process started")
    logger.info("org=%s source=%s", organisation_id, source_id)

    # Getting session and storage (sync DB session; async storage via run_async)
    organisation_id, source_id = str(organisation_id), str(source_id)
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    publisher = ProgressPublisher(organisation_id=organisation_id, source_id=source_id)

    try:
        # Load source record
        source: OrganisationSource | None = session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Source %s not found", source_id)
            return

        # Skip if not in expected state
        if source.status == OrganisationSourceStatus.PENDING_UPLOAD:
            logger.warning("Source %s still pending upload; skipping processing run", source_id)
            return

        # Locate job for this step
        job: KnowledgeProcessingJob | None = (
            session.query(KnowledgeProcessingJob)
            .filter_by(source_id=source_id, job_type="post_upload_process")
            .first()
        )

        if not job:
            logger.warning("No post_upload_process job found for source %s; skipping", source_id)
            return

        # Mark job as in progress and persist early
        job.status = JobStatus.IN_PROGRESS
        session.add(job)
        session.commit()

        # Compute checksum early (mirrors chat worker style)
        _compute_and_persist_checksum(storage, source, session)

        # Duplicate detection and handling
        if _handle_duplicate_if_any(session, source, job, publisher):
            return

        # Thumbnail generation using shared service
        try:
            created = run_async(
                generate_thumbnails(
                    storage=storage,
                    original_blob_path=source.storage_path or "",
                    mimetype=source.mimetype,
                )
            )
            _assign_generated_thumbnails(source, created)

            job.status = JobStatus.COMPLETED
            session.add(job)
            source.status = OrganisationSourceStatus.EXTRACTING
            session.merge(source)
            session.commit()

            # Generate SAS URLs for newly created thumbnails (if present)
            thumbnail_small_url = None
            thumbnail_medium_url = None
            if source.thumbnail_small_path or source.thumbnail_medium_path:
                thumbnail_small_url, thumbnail_medium_url = run_async(
                    generate_thumbnail_sas_urls(
                        storage=storage,
                        small_path=source.thumbnail_small_path,
                        medium_path=source.thumbnail_medium_path,
                        minutes=120,
                    )
                )

            publisher.publish(
                status=OrganisationSourceStatus.EXTRACTING.value,
                thumbnail_small_url=thumbnail_small_url,
                thumbnail_medium_url=thumbnail_medium_url,
            )

            # Note: args must be strings for JSON serialization
            app.send_task(
                "knowledge.document_extraction",
                args=[str(organisation_id), str(source_id)],
                queue="knowledge_extract",
                headers=correlation_headers(),
            )
        except self.MaxRetriesExceededError:
            logger.warning("Max retries exceeded for thumbnail generation source=%s", source_id)
            job.status = JobStatus.FAILED
            session.add(job)
            session.commit()
        except Exception as e:
            logger.warning("Thumbnail generation failed for source %s: %s", source_id, e)
            job.status = JobStatus.FAILED
            session.add(job)
            session.commit()
        logger.info("post_upload_process finished source=%s", source_id)
    except Exception as e:
        logger.exception(
            "knowledge post_upload_process failed org=%s source=%s error=%s",
            organisation_id,
            source_id,
            e,
        )
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
        # Close async Azure clients using persistent thread-local event loop
        with contextlib.suppress(Exception):
            run_async(storage.close())
