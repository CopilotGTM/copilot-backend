from __future__ import annotations

"""Heavy document extraction task (PDF/DOC extraction).

This task uses the v2 extraction architecture:
- ExtractionService orchestrates the pipeline
- ParserRegistry selects parser by MIME type
- ChunkProcessor handles multimodal processing + embeddings
- DocumentStore persists OrganisationDocuments
"""
import asyncio
import contextlib
import os
from uuid import UUID

from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import SoftTimeLimitExceeded
from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.database.sessions import acquire_org_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.models import ProcessingContext
from src.workers.knowledge_worker.services import ExtractionService
from src.workers.knowledge_worker.services.progress import ProgressPublisher
from src.workers.knowledge_worker.utils import (
    DocumentStore,
    download_blob_to_temp,
    mark_job_in_progress,
    set_job_status,
)

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
    """Validate mimetype, supported types, and storage path.

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


def _extract_document(
    organisation_id: str,
    session,
    source: OrganisationSource,
    job: KnowledgeProcessingJob | None,
    temp_file_path: str,
    publisher: ProgressPublisher,
) -> None:
    """
    Extract document using ExtractionService.

    Pipeline:
    1. ExtractionService selects parser via ParserRegistry
    2. Parser returns ParsedContent (raw chunks)
    3. ChunkProcessor handles multimodal + embeddings
    4. DocumentStore persists OrganisationDocuments
    """
    mimetype = source.mimetype

    logger.info(
        "Starting extraction for source %s with MIME type %s",
        source.source_id,
        mimetype,
    )

    # Build processing context
    context = ProcessingContext(
        organisation_id=organisation_id,
        source_id=str(source.source_id),
        user_id=str(source.user_id),
        source_blob_path=source.storage_path,
        original_filename=source.filename,
        mimetype=mimetype,
    )

    # Create extraction service
    extraction_service = ExtractionService(organisation_id)

    # Check if mimetype is supported
    if not extraction_service.is_supported(mimetype):
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
        return

    # Perform extraction
    result = extraction_service.extract(temp_file_path, context)

    if not result.success:
        logger.error(
            "Extraction failed for source %s: %s",
            source.source_id,
            result.error_message,
        )
        _finalize_and_publish(
            session,
            publisher,
            source,
            job,
            source_status=OrganisationSourceStatus.FAILED,
            job_status=JobStatus.FAILED if job else None,
            error_message=result.error_message or "Failed to extract content",
            progress=None,
        )
        return

    parsed_documents = result.documents or []
    logger.info(
        "Successfully extracted %d chunks from source %s",
        len(parsed_documents),
        source.source_id,
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
            error_message="No content could be extracted from document",
            progress=None,
        )
        return

    # Store documents
    document_store = DocumentStore(organisation_id)

    # Remove any previous chunks (idempotent re-run)
    try:
        deleted = document_store.delete_documents_by_source(str(source.source_id))
        logger.info("Deleted %s existing chunks for source %s", deleted, source.source_id)
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
    logger.info(f"Starting document extraction for source {source_id} in org {organisation_id}")

    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        source_id_str, organisation_id_str = str(source_id), str(organisation_id)
        publisher = ProgressPublisher(organisation_id=organisation_id_str, source_id=source_id_str)
        source, job = _load_source_and_job(session, source_id_str)
        if not source or not job:
            return

        mark_job_in_progress(session, job, source_id_str)
        publisher.publish(status=OrganisationSourceStatus.PROCESSING.value, progress=5)

        if not _validate_source_requirements(session, publisher, source, job):
            return

        logger.info("Downloading file from storage: %s", source.storage_path)
        temp_file_path = download_blob_to_temp(loop, storage, source)

        try:
            _extract_document(
                organisation_id_str,
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
        logger.error("Document extraction timed out for source %s: %s", source_id, str(e))
        _finalize_failure_by_ids(
            session,
            publisher,
            str(source_id),
            "Document extraction timed out after 30 minutes. "
            "The file may be too large or complex to process.",
        )
        return
    except Exception as e:
        logger.error("Error during document extraction for source %s: %s", source_id, str(e))
        _finalize_failure_by_ids(
            session,
            publisher,
            str(source_id),
            f"Document extraction failed: {str(e)}",
        )
        raise
    finally:
        # Close database session
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()

        # Close storage connection
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
