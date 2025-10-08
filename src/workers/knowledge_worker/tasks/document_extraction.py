from __future__ import annotations

"""Heavy document extraction task (PDF/DOC extraction).
"""
import asyncio
import os

from uuid import UUID
from celery.exceptions import SoftTimeLimitExceeded
from billiard.exceptions import TimeLimitExceeded

from src.workers.knowledge_worker.celery_app import app
from src.infra.utils.logger import get_logger
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.domain.models.organisation_tables.job_models import (
    KnowledgeProcessingJob,
    JobStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.workers.knowledge_worker.parsers import parser_factory
from src.workers.knowledge_worker.utils import (
    DocumentStore,
    mark_job_in_progress,
    set_job_status,
    download_blob_to_temp,
)
from src.workers.knowledge_worker.services.progress import ProgressPublisher

logger = get_logger("knowledge_document_extraction")


def _update_status(
    session,
    source: OrganisationSource,
    job: KnowledgeProcessingJob | None,
    *,
    source_status: OrganisationSourceStatus,
    job_status: JobStatus | None,
    error_message: str | None = None,
):
    """Update source/job status and commit with basic safety."""
    source.status = source_status
    source.error_message = error_message
    session.add(source)
    if job and job_status is not None:
        set_job_status(job, job_status)
        session.add(job)
    session.commit()


def _finalize_and_publish(
    session,
    publisher: ProgressPublisher,
    source: OrganisationSource,
    job: KnowledgeProcessingJob | None,
    *,
    source_status: OrganisationSourceStatus,
    job_status: JobStatus | None,
    error_message: str | None = None,
    progress: int | None = None,
):
    """Helper to set statuses, commit, and publish an event."""
    try:
        _update_status(
            session,
            source,
            job,
            source_status=source_status,
            job_status=job_status,
            error_message=error_message,
        )
    except Exception:
        session.rollback()
        raise
    publisher.publish(
        status=source_status.value,
        progress=progress,
    )


def _load_source_and_job(
    session,
    source_id: str,
) -> tuple[OrganisationSource | None, KnowledgeProcessingJob | None]:
    """Load source and associated document_extraction job, with logging."""
    source = session.get(OrganisationSource, source_id)
    if not source:
        logger.warning("Source %s not found for extract", source_id)
        return None, None

    job = (
        session.query(KnowledgeProcessingJob)
        .filter_by(source_id=source_id, job_type="document_extraction")
        .first()
    )
    if not job:
        logger.warning("No document_extraction job found for source %s", source_id)
        return source, None

    return source, job


def _finalize_failure_by_ids(
    session,
    publisher: ProgressPublisher,
    source_id: str,
    message: str,
) -> None:
    """Fetch source/job and publish a FAILED status with the given message."""
    source = session.get(OrganisationSource, source_id)
    if not source:
        logger.warning("Cannot finalize failure; source %s not found", source_id)
        return
    job = (
        session.query(KnowledgeProcessingJob)
        .filter_by(source_id=source_id, job_type="document_extraction")
        .first()
    )
    _finalize_and_publish(
        session,
        publisher,
        source,
        job,
        source_status=OrganisationSourceStatus.FAILED,
        job_status=JobStatus.FAILED if job else None,
        error_message=message,
        progress=None,
    )


def _validate_source_requirements(
    session,
    publisher: ProgressPublisher,
    source: OrganisationSource,
    job: KnowledgeProcessingJob | None,
) -> bool:
    """Validate mimetype, supported types, and storage path. Publish on failure.

    Returns True if all validations pass; otherwise publishes failure and returns False.
    """
    mimetype = source.mimetype
    if not mimetype:
        logger.error("No MIME type found for source %s", source.source_id)
        _finalize_and_publish(
            session,
            publisher,
            source,
            job,
            source_status=OrganisationSourceStatus.FAILED,
            job_status=JobStatus.FAILED if job else None,
            error_message="No MIME type specified",
            progress=None,
        )
        return False

    supported_types = parser_factory.get_supported_mimetypes()
    if mimetype.lower() not in supported_types:
        logger.warning("No parser available for MIME type %s", mimetype)
        _finalize_and_publish(
            session,
            publisher,
            source,
            job,
            source_status=OrganisationSourceStatus.FAILED,
            job_status=JobStatus.FAILED if job else None,
            error_message=f"Unsupported document type: {mimetype}",
            progress=None,
        )
        return False

    if not source.storage_path:
        logger.error("No storage path found for source %s", source.source_id)
        _finalize_and_publish(
            session,
            publisher,
            source,
            job,
            source_status=OrganisationSourceStatus.FAILED,
            job_status=JobStatus.FAILED if job else None,
            error_message="No storage path found",
            progress=None,
        )
        return False

    return True


def _parse_store_finalize(
    storage: AzureOrganisationBlobStorage,
    organisation_id: str,
    session,
    source: OrganisationSource,
    job: KnowledgeProcessingJob | None,
    temp_file_path: str,
    publisher: ProgressPublisher,
) -> None:
    """Parse the document, write chunks, and finalize with appropriate status."""
    mimetype = source.mimetype

    logger.info(
        "Starting document parsing for source %s with MIME type %s",
        source.source_id,
        mimetype,
    )

    parsed_documents = parser_factory.parse_document(
        file_path=temp_file_path,
        mimetype=mimetype,
        organisation_id=organisation_id,
        user_id=str(source.user_id),
        storage_backend=storage,
        document_url=source.filename,
        source_id=str(source.source_id),
        source_blob_path=source.storage_path,
        original_filename=source.filename,
        source_size_bytes=(
            source.size_bytes if hasattr(source, "size_bytes") else None
        ),
    )

    if not parsed_documents:
        logger.error("No content extracted from document %s", source.source_id)
        _finalize_and_publish(
            session,
            publisher,
            source,
            job,
            source_status=OrganisationSourceStatus.FAILED,
            job_status=JobStatus.FAILED if job else None,
            error_message="Failed to extract content from document",
            progress=None,
        )
        return

    logger.info(
        "Successfully parsed document %s, extracted %d chunks",
        source.source_id,
        len(parsed_documents),
    )

    # Remove any previous chunks for this source (idempotent re-run), then store
    document_store = DocumentStore(organisation_id)
    try:
        deleted = document_store.delete_documents_by_source(str(source.source_id))
        logger.info(
            "Deleted %s existing chunks for source %s", deleted, source.source_id
        )
    except Exception as e:
        logger.warning(
            "Failed to delete existing chunks for source %s: %s",
            source.source_id,
            e,
        )

    stored_ids = document_store.store_documents(
        parsed_documents,
        str(source.source_id),
        str(source.user_id) if source and source.user_id else None,
    )

    if not stored_ids:
        # Distinguish between 'parsed but duplicates' and true failure based on parsed_documents presence
        logger.warning(
            "No new unique document chunks stored for source %s (all duplicates)",
            source.source_id,
        )
        _finalize_and_publish(
            session,
            publisher,
            source,
            job,
            source_status=OrganisationSourceStatus.COMPLETED,
            job_status=JobStatus.COMPLETED if job else None,
            error_message=None,
            progress=100,
        )
        return

    logger.info(
        "Successfully stored %d document chunks for source %s",
        len(stored_ids),
        source.source_id,
    )

    # Update status to completed and publish
    _finalize_and_publish(
        session,
        publisher,
        source,
        job,
        source_status=OrganisationSourceStatus.COMPLETED,
        job_status=JobStatus.COMPLETED if job else None,
        error_message=None,
        progress=100,
    )


@app.task(
    name="knowledge.document_extraction",
    bind=True,
    acks_late=True,
    max_retries=3,
    time_limit=1800,  # 30 minutes hard limit
    soft_time_limit=1680,  # 28 minutes soft limit
)
def document_extraction(self, organisation_id: UUID, source_id: UUID):
    """Heavy extraction task (PDF/DOC etc.)."""
    logger.info(
        f"Starting document extraction for source {source_id} in org {organisation_id}"
    )

    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        source_id, organisation_id = str(source_id), str(organisation_id)
        publisher = ProgressPublisher(
            organisation_id=organisation_id, source_id=source_id
        )
        source, job = _load_source_and_job(session, source_id)
        if not source or not job:
            return

        mark_job_in_progress(session, job, source_id)

        if not _validate_source_requirements(session, publisher, source, job):
            return

        logger.info("Downloading file from storage: %s", source.storage_path)
        temp_file_path = download_blob_to_temp(loop, storage, source)

        try:
            _parse_store_finalize(
                storage,
                organisation_id,
                session,
                source,
                job,
                temp_file_path,
                publisher,
            )
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                    logger.debug("Cleaned up temporary file: %s", temp_file_path)
                except Exception as cleanup_err:
                    logger.warning(
                        "Failed to clean up temporary file %s: %s",
                        temp_file_path,
                        str(cleanup_err),
                    )

    except (SoftTimeLimitExceeded, TimeLimitExceeded) as e:
        logger.error(
            "Document extraction timed out for source %s: %s", source_id, str(e)
        )
        _finalize_failure_by_ids(
            session,
            publisher,
            source_id,
            "Document extraction timed out after 30 minutes. The file may be too large or complex to process.",
        )
        return
    except Exception as e:
        logger.error(
            "Error during document extraction for source %s: %s", source_id, str(e)
        )
        _finalize_failure_by_ids(
            session,
            publisher,
            source_id,
            f"Document extraction failed: {str(e)}",
        )
        raise
    finally:
        # Close database session
        try:
            session.close()
        except Exception:
            pass
        try:
            session_gen.close()
        except Exception:
            pass

        # Close storage connection
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
