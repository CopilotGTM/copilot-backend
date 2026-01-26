"""Stale job cleanup service for knowledge worker.

This service handles cleanup of knowledge processing jobs that have been
stuck in non-terminal states:
- PENDING for more than 2 hours → mark as FAILED
- IN_PROGRESS for more than 24 hours → mark as FAILED

Follows the fresh session pattern - sessions are acquired only when needed
for database operations and released immediately after.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlmodel import col

from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("stale_job_cleanup_service")

# Threshold configurations for different job states
PENDING_THRESHOLD_HOURS = 2  # Jobs pending for more than 2 hours
IN_PROGRESS_THRESHOLD_HOURS = 24  # Jobs in progress for more than 24 hours


@dataclass
class CleanupResult:
    """Result of cleanup operation for a specific status."""

    status: JobStatus
    threshold_hours: int
    jobs_found: int
    jobs_marked_failed: int


class StaleJobCleanupService:
    """Service for cleaning up stale knowledge processing jobs.

    A job is considered stale based on its status:
    - PENDING: stuck for more than 2 hours from created_at
    - IN_PROGRESS: stuck for more than 24 hours from created_at
    """

    def __init__(self, organisation_id: UUID):
        """Initialize the service.

        Args:
            organisation_id: The organisation UUID
        """
        self.organisation_id = organisation_id

    def cleanup_stale_jobs(self) -> dict:
        """Main entry point - cleanup stale jobs for this organisation.

        Returns:
            dict with cleanup results
        """
        return run_async(self._cleanup_stale_jobs_async())

    async def _cleanup_stale_jobs_async(self) -> dict:
        """Async implementation of stale job cleanup."""
        now = datetime.now(UTC)

        # Cleanup PENDING jobs (threshold: 2 hours)
        pending_result = await self._cleanup_jobs_by_status(
            status=JobStatus.PENDING,
            threshold_hours=PENDING_THRESHOLD_HOURS,
            now=now,
        )

        # Cleanup IN_PROGRESS jobs (threshold: 24 hours)
        in_progress_result = await self._cleanup_jobs_by_status(
            status=JobStatus.IN_PROGRESS,
            threshold_hours=IN_PROGRESS_THRESHOLD_HOURS,
            now=now,
        )

        total_found = pending_result.jobs_found + in_progress_result.jobs_found
        total_marked = pending_result.jobs_marked_failed + in_progress_result.jobs_marked_failed

        logger.info(
            "Stale job cleanup completed for org=%s: "
            "pending (>%dh): found=%d, marked=%d | "
            "in_progress (>%dh): found=%d, marked=%d",
            self.organisation_id,
            PENDING_THRESHOLD_HOURS,
            pending_result.jobs_found,
            pending_result.jobs_marked_failed,
            IN_PROGRESS_THRESHOLD_HOURS,
            in_progress_result.jobs_found,
            in_progress_result.jobs_marked_failed,
        )

        return {
            "organisation_id": str(self.organisation_id),
            "total_stale_jobs_found": total_found,
            "total_jobs_marked_failed": total_marked,
            "pending_jobs": {
                "threshold_hours": PENDING_THRESHOLD_HOURS,
                "found": pending_result.jobs_found,
                "marked_failed": pending_result.jobs_marked_failed,
            },
            "in_progress_jobs": {
                "threshold_hours": IN_PROGRESS_THRESHOLD_HOURS,
                "found": in_progress_result.jobs_found,
                "marked_failed": in_progress_result.jobs_marked_failed,
            },
        }

    async def _cleanup_jobs_by_status(
        self,
        status: JobStatus,
        threshold_hours: int,
        now: datetime,
    ) -> CleanupResult:
        """Cleanup stale jobs for a specific status.

        Args:
            status: The job status to check (PENDING or IN_PROGRESS)
            threshold_hours: Hours after which jobs are considered stale
            now: Current timestamp for cutoff calculation

        Returns:
            CleanupResult with statistics
        """
        cutoff_time = now - timedelta(hours=threshold_hours)
        stale_job_ids: list[str] = []
        updated_count = 0

        # Phase 1: Find stale jobs for this status
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(col(KnowledgeProcessingJob.job_id)).where(
                col(KnowledgeProcessingJob.status) == status,
                col(KnowledgeProcessingJob.created_at) < cutoff_time,
            )
            result = await session.execute(stmt)
            stale_job_ids = [str(row[0]) for row in result.all()]

        if not stale_job_ids:
            return CleanupResult(
                status=status,
                threshold_hours=threshold_hours,
                jobs_found=0,
                jobs_marked_failed=0,
            )

        logger.info(
            "Found %d stale %s jobs (>%d hours) for org=%s",
            len(stale_job_ids),
            status.value,
            threshold_hours,
            self.organisation_id,
        )

        # Phase 2: Mark stale jobs as failed
        async for session in acquire_org_async_session(self.organisation_id):
            error_message = (
                f"Job automatically marked as failed: was in {status.value} state for more than "
                f"{threshold_hours} hours without progress (cleanup at {now.isoformat()})"
            )

            update_stmt = (
                update(KnowledgeProcessingJob)
                .where(
                    col(KnowledgeProcessingJob.job_id).in_([UUID(jid) for jid in stale_job_ids]),
                    # Double-check status hasn't changed (idempotency)
                    col(KnowledgeProcessingJob.status) == status,
                )
                .values(
                    status=JobStatus.FAILED,
                    error_message=error_message,
                )
            )
            result = await session.execute(update_stmt)
            # rowcount exists on CursorResult but type stubs don't expose it
            updated_count = getattr(result, "rowcount", len(stale_job_ids))
            await session.commit()

        return CleanupResult(
            status=status,
            threshold_hours=threshold_hours,
            jobs_found=len(stale_job_ids),
            jobs_marked_failed=updated_count,
        )
