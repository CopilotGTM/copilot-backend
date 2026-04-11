"""Stale job cleanup service for deal worker.

This service handles cleanup of deal jobs that have been stuck in non-terminal states:
- PENDING for more than 2 hours → mark as FAILED
- IN_PROGRESS for more than 35 minutes → mark as FAILED

Different job types may have different threshold requirements, so we handle
cleanup per job type to allow for flexibility.

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
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("deal_stale_job_cleanup_service")

# Default threshold configurations for different job states
DEFAULT_PENDING_THRESHOLD_HOURS = 2  # Jobs pending for more than 2 hours
DEFAULT_IN_PROGRESS_THRESHOLD_HOURS = 0.58  # Jobs in progress for more than 35 minutes

# Job type specific thresholds (override defaults if needed)
# Values are in hours (supports fractional hours, e.g., 0.58 = 35 minutes)
JOB_TYPE_THRESHOLDS: dict[DealJobType, dict[str, float]] = {
    # Meeting jobs
    DealJobType.MEETING_BRIEFING: {"pending": 1, "in_progress": 0.58},
    DealJobType.MEETING_SUMMARY: {"pending": 1, "in_progress": 0.58},
    DealJobType.MEETING_FOLLOWUP: {"pending": 1, "in_progress": 0.58},
    # Stakeholder jobs
    DealJobType.STAKEHOLDER_ANALYSIS: {"pending": 2, "in_progress": 0.58},
    DealJobType.STAKEHOLDER_ENRICHMENT: {"pending": 2, "in_progress": 0.58},
    # Deal-level jobs
    DealJobType.DEAL_SUMMARY: {"pending": 2, "in_progress": 0.58},
    DealJobType.DEAL_RISK_ANALYSIS: {"pending": 2, "in_progress": 0.58},
    DealJobType.DEAL_NEXT_STEPS: {"pending": 2, "in_progress": 0.58},
    # Risk/Blocker extraction
    DealJobType.RISK_BLOCKER_EXTRACTION: {"pending": 2, "in_progress": 0.58},
    # Content generation
    DealJobType.PROPOSAL_DRAFT: {"pending": 2, "in_progress": 0.58},
    DealJobType.EMAIL_DRAFT: {"pending": 2, "in_progress": 0.58},
    # Deal import
    DealJobType.DEAL_IMPORT: {"pending": 3, "in_progress": 0.58},
}


@dataclass
class JobTypeCleanupResult:
    """Result of cleanup operation for a specific job type and status."""

    job_type: DealJobType
    status: JobStatus
    threshold_hours: float
    jobs_found: int
    jobs_marked_failed: int


@dataclass
class CleanupSummary:
    """Summary of cleanup operation across all job types."""

    total_jobs_found: int
    total_jobs_marked_failed: int
    by_job_type: dict[str, dict]


class DealStaleJobCleanupService:
    """Service for cleaning up stale deal jobs.

    A job is considered stale based on its status and job type:
    - PENDING: stuck for more than the configured threshold from created_at
    - IN_PROGRESS: stuck for more than the configured threshold from created_at

    Different job types may have different thresholds to account for
    varying expected completion times (e.g., LLM-heavy jobs take longer).
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
        """Async implementation of stale job cleanup.

        Iterates through all job types and cleans up stale jobs for each.
        """
        now = datetime.now(UTC)
        results_by_job_type: dict[str, dict] = {}
        total_found = 0
        total_marked = 0

        # Process each job type separately
        for job_type in DealJobType:
            thresholds = JOB_TYPE_THRESHOLDS.get(
                job_type,
                {
                    "pending": DEFAULT_PENDING_THRESHOLD_HOURS,
                    "in_progress": DEFAULT_IN_PROGRESS_THRESHOLD_HOURS,
                },
            )

            # Cleanup PENDING jobs for this job type
            pending_result = await self._cleanup_jobs_by_type_and_status(
                job_type=job_type,
                status=JobStatus.PENDING,
                threshold_hours=thresholds["pending"],
                now=now,
            )

            # Cleanup IN_PROGRESS jobs for this job type
            in_progress_result = await self._cleanup_jobs_by_type_and_status(
                job_type=job_type,
                status=JobStatus.IN_PROGRESS,
                threshold_hours=thresholds["in_progress"],
                now=now,
            )

            type_found = pending_result.jobs_found + in_progress_result.jobs_found
            type_marked = pending_result.jobs_marked_failed + in_progress_result.jobs_marked_failed

            # Only include in results if there was something to clean up
            if type_found > 0:
                results_by_job_type[job_type.value] = {
                    "pending_jobs": {
                        "threshold_hours": thresholds["pending"],
                        "found": pending_result.jobs_found,
                        "marked_failed": pending_result.jobs_marked_failed,
                    },
                    "in_progress_jobs": {
                        "threshold_hours": thresholds["in_progress"],
                        "found": in_progress_result.jobs_found,
                        "marked_failed": in_progress_result.jobs_marked_failed,
                    },
                    "total_found": type_found,
                    "total_marked_failed": type_marked,
                }

            total_found += type_found
            total_marked += type_marked

        if total_found > 0:
            logger.info(
                "Stale deal job cleanup completed for org=%s: total_found=%d, total_marked_failed=%d",
                self.organisation_id,
                total_found,
                total_marked,
                extra={
                    "event": "stale_job_cleanup_completed",
                    "organisation_id": str(self.organisation_id),
                    "jobs_found": total_found,
                    "jobs_marked_failed": total_marked,
                },
            )
        else:
            logger.debug(
                "No stale deal jobs found for org=%s",
                self.organisation_id,
            )

        return {
            "organisation_id": str(self.organisation_id),
            "total_stale_jobs_found": total_found,
            "total_jobs_marked_failed": total_marked,
            "by_job_type": results_by_job_type,
        }

    async def _cleanup_jobs_by_type_and_status(
        self,
        job_type: DealJobType,
        status: JobStatus,
        threshold_hours: float,
        now: datetime,
    ) -> JobTypeCleanupResult:
        """Cleanup stale jobs for a specific job type and status.

        Args:
            job_type: The deal job type to check
            status: The job status to check (PENDING or IN_PROGRESS)
            threshold_hours: Hours after which jobs are considered stale
            now: Current timestamp for cutoff calculation

        Returns:
            JobTypeCleanupResult with statistics
        """
        cutoff_time = now - timedelta(hours=threshold_hours)
        stale_job_ids: list[UUID] = []
        updated_count = 0

        # Phase 1: Find stale jobs for this job type and status
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(col(DealJobs.job_id)).where(
                col(DealJobs.job_type) == job_type,
                col(DealJobs.status) == status,
                col(DealJobs.created_at) < cutoff_time,
            )
            result = await session.execute(stmt)
            stale_job_ids = [row[0] for row in result.all()]

        # Session released here (fresh session pattern)

        if not stale_job_ids:
            return JobTypeCleanupResult(
                job_type=job_type,
                status=status,
                threshold_hours=threshold_hours,
                jobs_found=0,
                jobs_marked_failed=0,
            )

        logger.info(
            "Found %d stale %s jobs (type=%s, >%d hours) for org=%s",
            len(stale_job_ids),
            status.value,
            job_type.value,
            threshold_hours,
            self.organisation_id,
            extra={
                "event": "stale_job_cleanup_jobs_found",
                "organisation_id": str(self.organisation_id),
                "job_type": job_type.value,
                "status": status.value,
                "jobs_found": len(stale_job_ids),
                "threshold_hours": threshold_hours,
            },
        )

        # Phase 2: Mark stale jobs as failed
        async for session in acquire_org_async_session(self.organisation_id):
            error_message = (
                f"Job automatically marked as failed: was in {status.value} state for more than "
                f"{threshold_hours} hours without progress (cleanup at {now.isoformat()})"
            )

            update_stmt = (
                update(DealJobs)
                .where(
                    col(DealJobs.job_id).in_(stale_job_ids),
                    # Double-check status and type haven't changed (idempotency)
                    col(DealJobs.job_type) == job_type,
                    col(DealJobs.status) == status,
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

        # Session released here (fresh session pattern)

        return JobTypeCleanupResult(
            job_type=job_type,
            status=status,
            threshold_hours=threshold_hours,
            jobs_found=len(stale_job_ids),
            jobs_marked_failed=updated_count,
        )
