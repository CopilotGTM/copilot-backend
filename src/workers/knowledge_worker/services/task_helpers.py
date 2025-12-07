"""Task helper service - orchestrates status management for extraction tasks.

This module extracts status update and validation logic from document_extraction.py
to provide a cleaner separation between Celery task scheduling and business logic.

All status updates, validations, and error handling for document extraction tasks
are centralized here for reusability and testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.services.progress import ProgressPublisher
from src.workers.knowledge_worker.utils import set_job_status

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = get_logger("task_helpers")


class TaskHelperService:
    """
    Service for managing document extraction task status and validation.

    This service encapsulates all the helper functions needed by the
    document_extraction Celery task, providing:
    - Source and job loading
    - Status updates with database commits
    - Progress publishing via Redis
    - Validation of source requirements

    Usage:
        helper = TaskHelperService(session, publisher)
        source, job = helper.load_source_and_job(source_id)
        if not helper.validate_source_requirements(source, job):
            return
        # ... do extraction ...
        helper.finalize_success(source, job)
    """

    def __init__(self, session: Session, publisher: ProgressPublisher):
        """
        Initialize the task helper service.

        Args:
            session: SQLAlchemy database session
            publisher: Redis progress publisher for SSE updates
        """
        self._session = session
        self._publisher = publisher

    def load_source_and_job(
        self,
        source_id: str,
    ) -> tuple[OrganisationSource | None, KnowledgeProcessingJob | None]:
        """
        Load source and associated document_extraction job.

        Args:
            source_id: The source ID to load

        Returns:
            Tuple of (source, job), either may be None if not found
        """
        source = self._session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Source %s not found for extract", source_id)
            return None, None

        job = (
            self._session.query(KnowledgeProcessingJob)
            .filter_by(source_id=source_id, job_type="document_extraction")
            .first()
        )
        if not job:
            logger.warning("No document_extraction job found for source %s", source_id)
            return source, None

        return source, job

    def update_status(
        self,
        source: OrganisationSource,
        job: KnowledgeProcessingJob | None,
        *,
        source_status: OrganisationSourceStatus,
        job_status: JobStatus | None,
        error_message: str | None = None,
    ) -> None:
        """
        Update source/job status and commit with basic safety.

        Args:
            source: The source to update
            job: The job to update (optional)
            source_status: New status for the source
            job_status: New status for the job (if provided)
            error_message: Error message to set (optional)
        """
        source.status = source_status
        source.error_message = error_message
        self._session.add(source)

        if job and job_status is not None:
            set_job_status(job, job_status)
            self._session.add(job)

        self._session.commit()

    def finalize_and_publish(
        self,
        source: OrganisationSource,
        job: KnowledgeProcessingJob | None,
        *,
        source_status: OrganisationSourceStatus,
        job_status: JobStatus | None,
        error_message: str | None = None,
        progress: int | None = None,
    ) -> None:
        """
        Set statuses, commit, and publish a progress event.

        This is the primary method for finalizing any state change,
        ensuring database and Redis are in sync.

        Args:
            source: The source to update
            job: The job to update (optional)
            source_status: New status for the source
            job_status: New status for the job
            error_message: Error message to set (optional)
            progress: Progress percentage (0-100)
        """
        try:
            self.update_status(
                source,
                job,
                source_status=source_status,
                job_status=job_status,
                error_message=error_message,
            )
        except Exception:
            self._session.rollback()
            raise

        self._publisher.publish(
            status=source_status.value,
            progress=progress,
        )

    def finalize_failure_by_id(
        self,
        source_id: str,
        message: str,
    ) -> None:
        """
        Fetch source/job and publish a FAILED status with the given message.

        This is used for error handling when we only have the source_id,
        not the actual source/job objects.

        Args:
            source_id: The source ID to look up
            message: Error message to record
        """
        source = self._session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Cannot finalize failure; source %s not found", source_id)
            return

        job = (
            self._session.query(KnowledgeProcessingJob)
            .filter_by(source_id=source_id, job_type="document_extraction")
            .first()
        )

        self.finalize_and_publish(
            source,
            job,
            source_status=OrganisationSourceStatus.FAILED,
            job_status=JobStatus.FAILED if job else None,
            error_message=message,
            progress=None,
        )

    def validate_source_requirements(
        self,
        source: OrganisationSource,
        job: KnowledgeProcessingJob | None,
    ) -> bool:
        """
        Validate mimetype and storage path requirements.

        If validation fails, publishes failure status and returns False.

        Args:
            source: The source to validate
            job: The associated job (for status updates)

        Returns:
            True if all validations pass, False otherwise
        """
        mimetype = source.mimetype
        if not mimetype:
            logger.error("No MIME type found for source %s", source.source_id)
            self.finalize_and_publish(
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
            self.finalize_and_publish(
                source,
                job,
                source_status=OrganisationSourceStatus.FAILED,
                job_status=JobStatus.FAILED if job else None,
                error_message="No storage path found",
                progress=None,
            )
            return False

        return True

    def finalize_success(
        self,
        source: OrganisationSource,
        job: KnowledgeProcessingJob | None,
    ) -> None:
        """
        Finalize with COMPLETED status and 100% progress.

        Args:
            source: The source to mark complete
            job: The associated job
        """
        self.finalize_and_publish(
            source,
            job,
            source_status=OrganisationSourceStatus.COMPLETED,
            job_status=JobStatus.COMPLETED if job else None,
            error_message=None,
            progress=100,
        )

    def finalize_failure(
        self,
        source: OrganisationSource,
        job: KnowledgeProcessingJob | None,
        error_message: str,
    ) -> None:
        """
        Finalize with FAILED status.

        Args:
            source: The source to mark failed
            job: The associated job
            error_message: The error message to record
        """
        self.finalize_and_publish(
            source,
            job,
            source_status=OrganisationSourceStatus.FAILED,
            job_status=JobStatus.FAILED if job else None,
            error_message=error_message,
            progress=None,
        )
