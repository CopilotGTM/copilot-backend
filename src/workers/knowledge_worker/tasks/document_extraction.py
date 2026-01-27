"""Heavy document extraction task (PDF/DOC extraction).

This task uses the extraction architecture:
- ExtractionService orchestrates the full pipeline (parsing + storage)
- ParserRegistry selects parser by MIME type
- ChunkProcessor handles multimodal processing + embeddings
- TaskHelperService manages status updates and validation

The task file contains only the Celery task definition.
All extraction logic is delegated to ExtractionService.

IMPORTANT: This task follows the fresh session pattern to avoid holding
database connections during long-running operations (file download, parsing,
embedding generation). Sessions are acquired only when needed for DB operations
and released before any slow external calls.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from uuid import UUID

from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

from src.domain.models.organisation_tables.knowledge_models import OrganisationSourceStatus
from src.infra.database.sessions import org_session_scope
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.models import ProcessingContext
from src.workers.knowledge_worker.services import ExtractionService, TaskHelperService
from src.workers.knowledge_worker.services.progress import ProgressPublisher
from src.workers.knowledge_worker.utils import download_blob_to_temp, mark_job_in_progress

logger = get_logger("knowledge_document_extraction")

# Task time limits (seconds)
HARD_TIME_LIMIT = 1800  # 30 minutes
SOFT_TIME_LIMIT = 1680  # 28 minutes

# Retry configuration
DEFAULT_RETRY_DELAY = 60  # 1 minute base delay for exponential backoff


@dataclass
class SourceData:
    """Data extracted from source record for processing without holding session."""

    source_id: str
    user_id: str
    storage_path: str
    filename: str
    mimetype: str


def _load_and_validate_source(
    org_uuid: UUID,
    source_id_str: str,
    publisher: ProgressPublisher,
) -> SourceData | None:
    """
    Load source data, validate, and mark job in-progress.

    Uses fresh session pattern: acquires session, extracts needed data, releases session.
    This prevents holding DB connections during the long extraction process.

    Returns:
        SourceData if valid, None if source/job not found or validation failed.
    """
    with org_session_scope(org_uuid) as session:
        helper = TaskHelperService(session, publisher)

        # Load source and job
        source, job = helper.load_source_and_job(source_id_str)
        if not source or not job:
            return None

        # Mark job in progress
        mark_job_in_progress(session, job, source_id_str)
        publisher.publish(status=OrganisationSourceStatus.EXTRACTING.value, progress=5)

        # Validate requirements
        if not helper.validate_source_requirements(source, job):
            return None

        # Extract data needed for processing (before releasing session)
        return SourceData(
            source_id=source_id_str,
            user_id=str(source.user_id),
            storage_path=source.storage_path or "",
            filename=source.filename or "",
            mimetype=source.mimetype or "",
        )


def _finalize_extraction(
    org_uuid: UUID,
    source_id_str: str,
    publisher: ProgressPublisher,
    *,
    success: bool,
    error_message: str | None = None,
    document_count: int = 0,
) -> None:
    """
    Finalize extraction with fresh session.

    Acquires a new session to update source/job status after extraction completes.
    """
    with org_session_scope(org_uuid) as session:
        helper = TaskHelperService(session, publisher)

        if success:
            logger.info(
                "Document extraction completed for source %s: %d chunks stored",
                source_id_str,
                document_count,
            )
            # Load source/job fresh for finalization
            source, job = helper.load_source_and_job(source_id_str)
            if source and job:
                helper.finalize_success(source, job)
        else:
            logger.error(
                "Extraction failed for source %s: %s",
                source_id_str,
                error_message,
            )
            helper.finalize_failure_by_id(
                source_id_str,
                error_message or "Failed to extract content",
            )


@app.task(
    name="knowledge.document_extraction",
    bind=True,
    acks_late=True,
    max_retries=3,
    default_retry_delay=DEFAULT_RETRY_DELAY,
    time_limit=HARD_TIME_LIMIT,
    soft_time_limit=SOFT_TIME_LIMIT,
)
def document_extraction(self, organisation_id: str, source_id: str):
    """
    Heavy extraction task for PDF/DOC documents.

    This Celery task orchestrates the full document extraction pipeline:
    1. Load source and job from database (fresh session, released after)
    2. Validate requirements (mimetype, storage path)
    3. Download file from blob storage (no session held)
    4. Extract and store content using ExtractionService (no session held during embeddings)
    5. Finalize status (fresh session)
    6. Publish progress updates via Redis

    Uses fresh session pattern: sessions are acquired only for DB operations and
    released before long-running operations (file download, parsing, embeddings).
    This prevents connection pool exhaustion during slow operations.

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        source_id: UUID of the source to process (string for JSON serialization)
    """
    logger.info(f"Starting document extraction for source {source_id} in org {organisation_id}")

    # Convert args to proper types (handle both str and UUID for robustness)
    org_uuid = organisation_id if isinstance(organisation_id, UUID) else UUID(organisation_id)
    source_id_str = str(source_id)
    organisation_id_str = str(organisation_id)

    # Initialize services (no DB session yet)
    publisher = ProgressPublisher(organisation_id=organisation_id_str, source_id=source_id_str)
    storage = AzureOrganisationBlobStorage(organisation_id=org_uuid)

    temp_file_path = None

    try:
        # Phase 1: Load and validate with fresh session (released after)
        source_data = _load_and_validate_source(org_uuid, source_id_str, publisher)
        if not source_data:
            return

        # Phase 2: Download file (no session held)
        logger.info("Downloading file from storage: %s", source_data.storage_path)

        # Create a minimal source object for download_blob_to_temp
        class _SourceForDownload:
            def __init__(self, storage_path: str, filename: str):
                self.storage_path = storage_path
                self.filename = filename

        temp_file_path = download_blob_to_temp(
            storage, _SourceForDownload(source_data.storage_path, source_data.filename)
        )

        # Phase 3: Extract and store (no session held during embeddings)
        context = ProcessingContext(
            organisation_id=organisation_id_str,
            source_id=source_id_str,
            user_id=source_data.user_id,
            source_blob_path=source_data.storage_path,
            original_filename=source_data.filename,
            mimetype=source_data.mimetype,
        )

        extraction_service = ExtractionService(organisation_id_str)

        # Check if mimetype is supported
        if not extraction_service.is_supported(source_data.mimetype):
            logger.warning("No parser available for MIME type %s", source_data.mimetype)
            _finalize_extraction(
                org_uuid,
                source_id_str,
                publisher,
                success=False,
                error_message=f"Unsupported document type: {source_data.mimetype}",
            )
            return

        # Extract and store documents (with progress updates)
        # This is the long-running operation - no session is held here
        result = extraction_service.extract_and_store(temp_file_path, context, publisher)

        # Phase 4: Finalize with fresh session
        _finalize_extraction(
            org_uuid,
            source_id_str,
            publisher,
            success=result.success,
            error_message=result.error_message,
            document_count=len(result.document_ids)
            if result.success and result.document_ids
            else 0,
        )

    except (SoftTimeLimitExceeded, TimeLimitExceeded) as e:
        # Timeout errors are non-retryable
        logger.error("Document extraction timed out for source %s: %s", source_id, str(e))
        _finalize_extraction(
            org_uuid,
            source_id_str,
            publisher,
            success=False,
            error_message=(
                f"Document extraction timed out after {HARD_TIME_LIMIT // 60} minutes. "
                "The file may be too large or complex to process."
            ),
        )
        return

    except MaxRetriesExceededError:
        # Final failure after all retries exhausted
        logger.error("Max retries exceeded for document extraction source %s", source_id)
        _finalize_extraction(
            org_uuid,
            source_id_str,
            publisher,
            success=False,
            error_message="Document extraction failed after maximum retry attempts.",
        )

    except Exception as e:
        logger.error("Error during document extraction for source %s: %s", source_id, str(e))
        # Retry with exponential backoff: 60s, 120s, 240s
        countdown = DEFAULT_RETRY_DELAY * (2**self.request.retries)
        logger.info(
            "Retrying document extraction for source %s in %d seconds (attempt %d/%d)",
            source_id,
            countdown,
            self.request.retries + 1,
            self.max_retries,
        )
        raise self.retry(exc=e, countdown=countdown) from e

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

        # Close storage using persistent thread-local event loop
        with contextlib.suppress(Exception):
            run_async(storage.close())
