"""Heavy document extraction task (PDF/DOC extraction).

This task uses the v2 extraction architecture:
- ExtractionService orchestrates the pipeline
- ParserRegistry selects parser by MIME type
- ChunkProcessor handles multimodal processing + embeddings
- DocumentStore persists OrganisationDocuments
- TaskHelperService manages status updates and validation

The task file contains only the Celery task definition and extraction logic.
All helper functions have been moved to TaskHelperService.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from uuid import UUID

from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import SoftTimeLimitExceeded
from src.domain.models.organisation_tables.knowledge_models import OrganisationSourceStatus
from src.infra.database.sessions import acquire_org_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.models import ProcessingContext
from src.workers.knowledge_worker.services import ExtractionService, TaskHelperService
from src.workers.knowledge_worker.services.progress import ProgressPublisher
from src.workers.knowledge_worker.utils import (
    DocumentStore,
    download_blob_to_temp,
    mark_job_in_progress,
)

logger = get_logger("knowledge_document_extraction")

# Task time limits (seconds)
HARD_TIME_LIMIT = 1800  # 30 minutes
SOFT_TIME_LIMIT = 1680  # 28 minutes


@app.task(
    name="knowledge.document_extraction",
    bind=True,
    acks_late=True,
    max_retries=3,
    time_limit=HARD_TIME_LIMIT,
    soft_time_limit=SOFT_TIME_LIMIT,
)
def document_extraction(self, organisation_id: UUID, source_id: UUID):
    """
    Heavy extraction task for PDF/DOC documents.

    This Celery task orchestrates the full document extraction pipeline:
    1. Load source and job from database
    2. Validate requirements (mimetype, storage path)
    3. Download file from blob storage
    4. Extract content using ExtractionService
    5. Store documents in database
    6. Publish progress updates via Redis

    Args:
        organisation_id: The organisation UUID
        source_id: The source UUID to process
    """
    logger.info(f"Starting document extraction for source {source_id} in org {organisation_id}")

    # Initialize infrastructure
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    source_id_str = str(source_id)
    organisation_id_str = str(organisation_id)

    # Initialize services
    publisher = ProgressPublisher(organisation_id=organisation_id_str, source_id=source_id_str)
    helper = TaskHelperService(session, publisher)

    try:
        # Load source and job
        source, job = helper.load_source_and_job(source_id_str)
        if not source or not job:
            return

        # Mark job in progress
        mark_job_in_progress(session, job, source_id_str)
        publisher.publish(status=OrganisationSourceStatus.PROCESSING.value, progress=5)

        # Validate requirements
        if not helper.validate_source_requirements(source, job):
            return

        # Download file from blob storage
        logger.info("Downloading file from storage: %s", source.storage_path)
        temp_file_path = download_blob_to_temp(loop, storage, source)

        try:
            # Run extraction pipeline
            _run_extraction_pipeline(
                organisation_id_str,
                source,
                job,
                temp_file_path,
                helper,
            )
        finally:
            # Cleanup temporary file
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
        helper.finalize_failure_by_id(
            source_id_str,
            f"Document extraction timed out after {HARD_TIME_LIMIT // 60} minutes. "
            "The file may be too large or complex to process.",
        )
        return

    except Exception as e:
        logger.error("Error during document extraction for source %s: %s", source_id, str(e))
        helper.finalize_failure_by_id(
            source_id_str,
            f"Document extraction failed: {str(e)}",
        )
        raise

    finally:
        # Cleanup resources
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()

        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()


def _run_extraction_pipeline(
    organisation_id: str,
    source,
    job,
    temp_file_path: str,
    helper: TaskHelperService,
) -> None:
    """
    Run the document extraction pipeline.

    Pipeline steps:
    1. ExtractionService selects parser via ParserRegistry
    2. Parser returns ParsedContent (raw chunks)
    3. ChunkProcessor handles multimodal + embeddings
    4. DocumentStore persists OrganisationDocuments

    Args:
        organisation_id: Organisation ID string
        source: OrganisationSource record
        job: KnowledgeProcessingJob record
        temp_file_path: Path to downloaded file
        helper: TaskHelperService for status updates
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
        helper.finalize_failure(source, job, f"Unsupported document type: {mimetype}")
        return

    # Perform extraction
    result = extraction_service.extract(temp_file_path, context)

    if not result.success:
        logger.error(
            "Extraction failed for source %s: %s",
            source.source_id,
            result.error_message,
        )
        helper.finalize_failure(source, job, result.error_message or "Failed to extract content")
        return

    parsed_documents = result.documents or []
    logger.info(
        "Successfully extracted %d chunks from source %s",
        len(parsed_documents),
        source.source_id,
    )

    if not parsed_documents:
        logger.error("No content extracted from document %s", source.source_id)
        helper.finalize_failure(source, job, "No content could be extracted from document")
        return

    # Store documents
    _store_extracted_documents(organisation_id, source, job, parsed_documents, helper)


def _store_extracted_documents(
    organisation_id: str,
    source,
    job,
    parsed_documents: list,
    helper: TaskHelperService,
) -> None:
    """
    Store extracted documents in the database.

    Args:
        organisation_id: Organisation ID string
        source: OrganisationSource record
        job: KnowledgeProcessingJob record
        parsed_documents: List of OrganisationDocument objects
        helper: TaskHelperService for status updates
    """
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

    # Store new documents
    stored_ids = document_store.store_documents(
        parsed_documents,
        str(source.source_id),
        str(source.user_id) if source.user_id else None,
    )

    if not stored_ids:
        logger.warning(
            "No new unique document chunks stored for source %s (all duplicates)",
            source.source_id,
        )
        helper.finalize_success(source, job)
        return

    logger.info(
        "Successfully stored %d document chunks for source %s",
        len(stored_ids),
        source.source_id,
    )

    helper.finalize_success(source, job)
