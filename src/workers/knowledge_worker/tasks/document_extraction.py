"""Heavy document extraction task (PDF/DOC extraction).

This task uses the extraction architecture:
- ExtractionService orchestrates the full pipeline (parsing + storage)
- ParserRegistry selects parser by MIME type
- ChunkProcessor handles multimodal processing + embeddings
- TaskHelperService manages status updates and validation

The task file contains only the Celery task definition.
All extraction logic is delegated to ExtractionService.
"""

from __future__ import annotations

import contextlib
import os
from uuid import UUID

from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.domain.models.organisation_tables.knowledge_models import OrganisationSourceStatus
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
    1. Load source and job from database
    2. Validate requirements (mimetype, storage path)
    3. Download file from blob storage
    4. Extract and store content using ExtractionService
    5. Publish progress updates via Redis

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        source_id: UUID of the source to process (string for JSON serialization)
    """
    logger.info(f"Starting document extraction for source {source_id} in org {organisation_id}")

    # Convert args to proper types (handle both str and UUID for robustness)
    org_uuid = organisation_id if isinstance(organisation_id, UUID) else UUID(organisation_id)
    source_id_str = str(source_id)
    organisation_id_str = str(organisation_id)

    # Initialize infrastructure
    # Note: acquire_org_session expects UUID per the function signature
    session_gen = acquire_org_session(org_uuid)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id_str)

    # Initialize services
    publisher = ProgressPublisher(organisation_id=organisation_id_str, source_id=source_id_str)
    helper = TaskHelperService(session, publisher)

    # Initialize references for exception handlers
    source = None
    job = None

    try:
        # Load source and job
        source, job = helper.load_source_and_job(source_id_str)
        if not source or not job:
            return

        # Mark job in progress
        mark_job_in_progress(session, job, source_id_str)
        publisher.publish(status=OrganisationSourceStatus.EXTRACTING.value, progress=5)

        # Validate requirements
        if not helper.validate_source_requirements(source, job):
            return

        # Download file from blob storage
        logger.info("Downloading file from storage: %s", source.storage_path)
        temp_file_path = download_blob_to_temp(storage, source)

        try:
            # Build processing context
            context = ProcessingContext(
                organisation_id=organisation_id_str,
                source_id=source_id_str,
                user_id=str(source.user_id),
                source_blob_path=source.storage_path,
                original_filename=source.filename,
                mimetype=source.mimetype,
            )

            # Create extraction service and run full pipeline
            extraction_service = ExtractionService(organisation_id_str)

            # Check if mimetype is supported
            if not extraction_service.is_supported(source.mimetype):
                logger.warning("No parser available for MIME type %s", source.mimetype)
                helper.finalize_failure(
                    source, job, f"Unsupported document type: {source.mimetype}"
                )
                return

            # Extract and store documents (with progress updates)
            result = extraction_service.extract_and_store(temp_file_path, context, publisher)

            if result.success:
                logger.info(
                    "Document extraction completed for source %s: %d chunks stored",
                    source_id_str,
                    len(result.document_ids),
                )
                helper.finalize_success(source, job)
            else:
                logger.error(
                    "Extraction failed for source %s: %s",
                    source_id_str,
                    result.error_message,
                )
                helper.finalize_failure(
                    source, job, result.error_message or "Failed to extract content"
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
        # Timeout errors are non-retryable
        logger.error("Document extraction timed out for source %s: %s", source_id, str(e))
        helper.finalize_failure_by_id(
            source_id_str,
            f"Document extraction timed out after {HARD_TIME_LIMIT // 60} minutes. "
            "The file may be too large or complex to process.",
        )
        return

    except MaxRetriesExceededError:
        # Final failure after all retries exhausted
        logger.error("Max retries exceeded for document extraction source %s", source_id)
        # Use source/job if available, otherwise fall back to ID-based update
        if source and job:
            helper.finalize_failure(
                source, job, "Document extraction failed after maximum retry attempts."
            )
        else:
            helper.finalize_failure_by_id(
                source_id_str,
                "Document extraction failed after maximum retry attempts.",
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
        # Cleanup resources
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()

        # Close storage using persistent thread-local event loop
        with contextlib.suppress(Exception):
            run_async(storage.close())
