"""Post-upload processing service for knowledge sources.

This service handles all business logic for source post-upload processing:
- Checksum computation
- Thumbnail generation
- Duplicate detection
- Status updates
- Triggering document extraction

IMPORTANT: This service follows the fresh session pattern. Sessions are acquired
only when needed for database operations and released immediately after.
No sessions are passed between methods or held during long-running operations.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from uuid import UUID

from sqlmodel import col

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

logger = get_logger("knowledge_post_upload_service")


@dataclass
class SourceData:
    """Data extracted from source record for processing."""

    source_id: str
    job_id: str
    storage_path: str
    mimetype: str
    checksum: str | None


@dataclass
class ProcessingResult:
    """Result of source processing."""

    checksum: str | None
    thumbnail_small_path: str | None
    thumbnail_medium_path: str | None
    failed: bool = False


class PostUploadService:
    """Service for handling knowledge source post-upload processing.

    This service encapsulates all business logic for post-upload processing:
    - Loading source data
    - Computing checksums
    - Generating thumbnails
    - Duplicate detection
    - Updating status
    - Triggering document extraction

    Sessions are acquired only when needed and released immediately.
    """

    def __init__(self, organisation_id: str | UUID):
        """Initialize the service.

        Args:
            organisation_id: The organisation UUID
        """
        self.organisation_id_str = str(organisation_id)
        self.org_uuid = (
            UUID(str(organisation_id)) if isinstance(organisation_id, str) else organisation_id
        )
        self.storage = AzureOrganisationBlobStorage(organisation_id=self.org_uuid)
        self.publisher: ProgressPublisher | None = None

    def process(self, source_id: str | UUID) -> bool:
        """Process a source: checksum, thumbnails, and trigger extraction.

        This is the main entry point for post-upload processing.

        Args:
            source_id: The source UUID

        Returns:
            True if processing succeeded, False otherwise
        """
        source_id_str = str(source_id)
        self.publisher = ProgressPublisher(
            organisation_id=self.organisation_id_str,
            source_id=source_id_str,
        )

        try:
            # Phase 1: Load source data (DB operation)
            source_data = self._load_source_data(source_id_str)
            if not source_data:
                return False

            # Phase 2: Process source (Azure operations - no DB)
            result = self._process_source(source_data)

            # Phase 3: Check for duplicates (DB operation)
            if self._check_duplicate(source_id_str, result.checksum):
                return True  # Duplicate handling is a valid completion

            # Phase 4: Finalize (DB operation)
            self._finalize(source_id_str, result)
            return not result.failed

        except Exception as e:
            logger.exception(
                "Knowledge post_upload_process failed org=%s source=%s error=%s",
                self.organisation_id_str,
                source_id_str,
                e,
            )
            self._mark_failed(source_id_str)
            return False

        finally:
            self._close_storage()

    def _load_source_data(self, source_id_str: str) -> SourceData | None:
        """Load source data from database.

        Acquires session, loads data, marks job in progress, releases session.
        """
        session_gen = acquire_org_session(self.org_uuid)
        session = next(session_gen)

        try:
            # Load source record
            source: OrganisationSource | None = session.get(OrganisationSource, source_id_str)
            if not source:
                logger.warning("Source %s not found", source_id_str)
                return None

            # Skip if not in expected state
            if source.status == OrganisationSourceStatus.PENDING_UPLOAD:
                logger.warning("Source %s still pending upload; skipping", source_id_str)
                return None

            # Locate job for this step
            job: KnowledgeProcessingJob | None = (
                session.query(KnowledgeProcessingJob)
                .filter_by(source_id=source_id_str, job_type="post_upload_process")
                .first()
            )

            if not job:
                logger.warning("No post_upload_process job found for source %s", source_id_str)
                return None

            # Mark job as in progress
            job.status = JobStatus.IN_PROGRESS
            session.add(job)
            session.commit()

            return SourceData(
                source_id=source_id_str,
                job_id=str(job.job_id),
                storage_path=source.storage_path or "",
                mimetype=source.mimetype or "",
                checksum=source.checksum,
            )
        finally:
            self._cleanup_session(session, session_gen)

    def _process_source(self, data: SourceData) -> ProcessingResult:
        """Process source: compute checksum and generate thumbnails.

        This method does NOT use any database sessions.
        All operations are Azure blob storage operations.
        """
        checksum = data.checksum
        thumbnail_small_path = None
        thumbnail_medium_path = None
        failed = False

        # Compute checksum if missing
        try:
            if data.storage_path and not checksum:
                checksum = run_async(self.storage.compute_checksum(data.storage_path))
        except Exception:
            logger.exception("Failed to compute checksum for source %s", data.source_id)

        # Generate thumbnails
        try:
            created = run_async(
                generate_thumbnails(
                    storage=self.storage,
                    original_blob_path=data.storage_path,
                    mimetype=data.mimetype,
                )
            )
            if created:
                thumbnail_small_path = created.get("thumb_small")
                thumbnail_medium_path = created.get("thumb_medium")
        except Exception as e:
            logger.warning("Thumbnail generation failed for source %s: %s", data.source_id, e)
            # Don't mark as failed - thumbnails are optional

        return ProcessingResult(
            checksum=checksum,
            thumbnail_small_path=thumbnail_small_path,
            thumbnail_medium_path=thumbnail_medium_path,
            failed=failed,
        )

    def _check_duplicate(self, source_id_str: str, checksum: str | None) -> bool:
        """Check for duplicate source by checksum.

        Acquires session, checks for duplicates, releases session.

        Returns:
            True if duplicate found and handled (caller should stop).
        """
        if not checksum:
            return False

        session_gen = acquire_org_session(self.org_uuid)
        session = next(session_gen)

        try:
            # Exclude soft-deleted and FAILED sources from duplicate consideration
            duplicate = (
                session.query(OrganisationSource)
                .filter(
                    col(OrganisationSource.checksum) == checksum,
                    col(OrganisationSource.source_id) != source_id_str,
                    col(OrganisationSource.is_deleted).is_(False),
                    col(OrganisationSource.status) != OrganisationSourceStatus.FAILED,
                )
                .first()
            )
            if not duplicate:
                return False

            # Found duplicate - mark source as failed
            source = session.get(OrganisationSource, source_id_str)
            job = (
                session.query(KnowledgeProcessingJob)
                .filter_by(source_id=source_id_str, job_type="post_upload_process")
                .first()
            )

            if source:
                source.status = OrganisationSourceStatus.FAILED
                source.error_message = (
                    f"Duplicate file detected; same content as source {duplicate.source_id}"
                )
                session.merge(source)

            if job:
                job.status = JobStatus.COMPLETED
                session.add(job)

            session.commit()

            # Publish failure status
            if self.publisher:
                self.publisher.publish(
                    status=OrganisationSourceStatus.FAILED.value,
                    error_message="file already exists",
                )

            logger.info(
                "Duplicate checksum found for source=%s; marked FAILED",
                source_id_str,
            )
            return True

        except Exception:
            logger.exception("Error during duplicate detection for source %s", source_id_str)
            return False
        finally:
            self._cleanup_session(session, session_gen)

    def _finalize(self, source_id_str: str, result: ProcessingResult) -> None:
        """Finalize processing: update database, publish status, trigger extraction.

        Acquires session, updates data, releases session.
        """
        session_gen = acquire_org_session(self.org_uuid)
        session = next(session_gen)

        try:
            source = session.get(OrganisationSource, source_id_str)
            job = (
                session.query(KnowledgeProcessingJob)
                .filter_by(source_id=source_id_str, job_type="post_upload_process")
                .first()
            )

            if not source or not job:
                logger.warning("Source or job not found for finalization: %s", source_id_str)
                return

            # Update source with processing results
            if result.checksum:
                source.checksum = result.checksum
            if result.thumbnail_small_path:
                source.thumbnail_small_path = result.thumbnail_small_path
            if result.thumbnail_medium_path:
                source.thumbnail_medium_path = result.thumbnail_medium_path

            if result.failed:
                job.status = JobStatus.FAILED
                session.add(job)
                session.commit()
                logger.info("post_upload_process finished with failure source=%s", source_id_str)
                return

            # Success path
            job.status = JobStatus.COMPLETED
            session.add(job)
            source.status = OrganisationSourceStatus.EXTRACTING
            session.merge(source)
            session.commit()

            # Store paths for SAS URL generation (after session release)
            thumb_small_path = source.thumbnail_small_path
            thumb_medium_path = source.thumbnail_medium_path

        finally:
            self._cleanup_session(session, session_gen)

        # Generate SAS URLs (outside of session)
        thumbnail_small_url, thumbnail_medium_url = self._generate_thumbnail_urls(
            thumb_small_path, thumb_medium_path
        )

        # Publish status
        if self.publisher:
            self.publisher.publish(
                status=OrganisationSourceStatus.EXTRACTING.value,
                thumbnail_small_url=thumbnail_small_url,
                thumbnail_medium_url=thumbnail_medium_url,
            )

        # Trigger document extraction task
        app.send_task(
            "knowledge.document_extraction",
            args=[self.organisation_id_str, source_id_str],
            queue="knowledge_extract",
            headers=correlation_headers(),
        )

        logger.info("post_upload_process finished source=%s", source_id_str)

    def _mark_failed(self, source_id_str: str) -> None:
        """Mark job as failed.

        Acquires session, updates status, releases session.
        """
        session_gen = acquire_org_session(self.org_uuid)
        session = next(session_gen)

        try:
            job = (
                session.query(KnowledgeProcessingJob)
                .filter_by(source_id=source_id_str, job_type="post_upload_process")
                .first()
            )
            if job:
                job.status = JobStatus.FAILED
                session.add(job)
                session.commit()
        except Exception:
            logger.debug(
                "Failed to persist FAILED state for source %s",
                source_id_str,
                exc_info=True,
            )
        finally:
            self._cleanup_session(session, session_gen)

    def _generate_thumbnail_urls(
        self, small_path: str | None, medium_path: str | None
    ) -> tuple[str | None, str | None]:
        """Generate SAS URLs for thumbnails."""
        if not small_path and not medium_path:
            return None, None

        try:
            return run_async(
                generate_thumbnail_sas_urls(
                    storage=self.storage,
                    small_path=small_path,
                    medium_path=medium_path,
                    minutes=120,
                )
            )
        except Exception:
            logger.debug("SAS URL generation failed", exc_info=True)
            return None, None

    def _cleanup_session(self, session, session_gen) -> None:
        """Clean up database session."""
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()  # type: ignore[union-attr]

    def _close_storage(self) -> None:
        """Close storage client."""
        try:
            close_coro = self.storage.close()
            if close_coro is not None:
                run_async(close_coro)
        except Exception:
            logger.debug("Failed to close storage client", exc_info=True)
