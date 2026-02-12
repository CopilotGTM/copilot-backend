"""Stale job cleanup service for knowledge worker.

Marks stuck sources and jobs as FAILED so the frontend stops showing
files as perpetually "processing" / "extracting" / "queued".

Three cleanup passes, each catching a different failure mode:

1. STALE SOURCES — the most common case.
   A file's source record has been sitting in an active status (PROCESSING,
   EXTRACTING, etc.) longer than its allowed threshold. This happens when the
   Celery task crashed, timed out, or was never picked up. We find these by
   checking source.updated_at vs a per-status time threshold, then mark both
   the source and any non-terminal jobs as FAILED.

2. ORPHANED SOURCES — a state-mismatch bug.
   The source still shows PROCESSING/EXTRACTING but ALL of its jobs have
   already reached a terminal state (COMPLETED/FAILED/CANCELLED). This means
   the job finished but the code path that ran it forgot (or failed) to update
   the source status. No time threshold needed — the mismatch alone tells us
   the source is stuck. We just set the source to FAILED.

3. STALE JOBS (safety net) — catches anything the first two passes missed.
   Individual jobs stuck in PENDING or IN_PROGRESS beyond a flat 2h threshold.
   This covers edge cases like: source.updated_at was refreshed (so pass #1
   skipped it) but the job itself is still stuck. We fail the job AND sync the
   parent source to FAILED so the frontend reflects the real state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select, update
from sqlmodel import col

from src.domain.models.organisation_tables.job_models import (
    JobStatus,
    KnowledgeProcessingJob,
)
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("stale_job_cleanup_service")

# (threshold_hours, user-facing error message) per active source status
SOURCE_THRESHOLDS: dict[OrganisationSourceStatus, tuple[int, str]] = {
    OrganisationSourceStatus.PENDING_UPLOAD: (24, "Upload was not completed in time"),
    OrganisationSourceStatus.UPLOADED: (2, "File uploaded but processing never started"),
    OrganisationSourceStatus.PROCESSING: (2, "File processing timed out"),
    OrganisationSourceStatus.CHUNKING: (3, "File chunking timed out"),
    OrganisationSourceStatus.EXTRACTING: (3, "File extraction timed out"),
}

# Flat threshold for the safety-net job pass
JOB_THRESHOLD_HOURS = 2

# Jobs in these statuses are "done" — we never touch them
TERMINAL_JOB_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)

# Every source status that should eventually reach COMPLETED or FAILED
ACTIVE_SOURCE_STATUSES = tuple(SOURCE_THRESHOLDS.keys())


class StaleJobCleanupService:
    """Cleans up stuck knowledge sources and jobs for one organisation.

    Called by the per-org Tier 2 Celery task (cleanup_stale_jobs_org_task).
    Each pass uses a fresh DB session so connections aren't held across passes.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    def cleanup_stale_jobs(self) -> dict:
        return run_async(self._run())

    async def _run(self) -> dict:
        now = datetime.now(UTC)
        counts: dict[str, int] = {
            "stale_sources": 0,
            "orphaned_sources": 0,
            "stale_jobs": 0,
        }

        # ── Pass 1: Stale sources ─────────────────────────────────────
        # For each active status, find sources whose updated_at is older than
        # the allowed threshold. Mark them + their non-terminal jobs as FAILED.
        for status, (hours, msg) in SOURCE_THRESHOLDS.items():
            cutoff = now - timedelta(hours=hours)
            error = f"{msg} (cleanup at {now.isoformat()})"

            async for session in acquire_org_async_session(self.organisation_id):
                source_ids = (
                    (
                        await session.execute(
                            select(col(OrganisationSource.source_id)).where(
                                col(OrganisationSource.status) == status,
                                col(OrganisationSource.is_deleted).is_(False),
                                col(OrganisationSource.updated_at) < cutoff,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                if not source_ids:
                    continue

                # Fail the sources
                await session.execute(
                    update(OrganisationSource)
                    .where(col(OrganisationSource.source_id).in_(source_ids))
                    .values(status=OrganisationSourceStatus.FAILED, error_message=error)
                )
                # Fail any of their jobs that haven't already finished
                await session.execute(
                    update(KnowledgeProcessingJob)
                    .where(
                        col(KnowledgeProcessingJob.source_id).in_(source_ids),
                        col(KnowledgeProcessingJob.status).notin_(TERMINAL_JOB_STATUSES),
                    )
                    .values(status=JobStatus.FAILED, error_message=error)
                )
                await session.commit()
                counts["stale_sources"] += len(source_ids)

                logger.info(
                    "Marked %d stale %s sources as FAILED (>%dh) for org=%s",
                    len(source_ids),
                    status.value,
                    hours,
                    self.organisation_id,
                )

        # ── Pass 2: Orphaned sources ──────────────────────────────────
        # Source is in an active status but ALL its jobs already finished.
        # The job completed/failed but never updated the parent source — fix it.
        async for session in acquire_org_async_session(self.organisation_id):
            # Source must have at least one job (otherwise it's just new, not orphaned)
            has_any_job = exists(
                select(col(KnowledgeProcessingJob.job_id)).where(
                    col(KnowledgeProcessingJob.source_id) == col(OrganisationSource.source_id)
                )
            )
            # Source must NOT have any still-running job
            has_active_job = exists(
                select(col(KnowledgeProcessingJob.job_id)).where(
                    col(KnowledgeProcessingJob.source_id) == col(OrganisationSource.source_id),
                    col(KnowledgeProcessingJob.status).notin_(TERMINAL_JOB_STATUSES),
                )
            )

            orphaned_ids = (
                (
                    await session.execute(
                        select(col(OrganisationSource.source_id)).where(
                            col(OrganisationSource.status).in_(ACTIVE_SOURCE_STATUSES),
                            col(OrganisationSource.is_deleted).is_(False),
                            has_any_job,
                            ~has_active_job,
                        )
                    )
                )
                .scalars()
                .all()
            )

            if orphaned_ids:
                error = (
                    "All jobs finished but file status was never updated"
                    f" (cleanup at {now.isoformat()})"
                )
                await session.execute(
                    update(OrganisationSource)
                    .where(col(OrganisationSource.source_id).in_(orphaned_ids))
                    .values(status=OrganisationSourceStatus.FAILED, error_message=error)
                )
                await session.commit()
                counts["orphaned_sources"] = len(orphaned_ids)

                logger.info(
                    "Fixed %d orphaned sources for org=%s",
                    len(orphaned_ids),
                    self.organisation_id,
                )

        # ── Pass 3: Stale jobs (safety net) ───────────────────────────
        # Catches jobs that pass 1 missed (e.g. source.updated_at was refreshed
        # but the job itself is still stuck). Fails the job AND syncs its parent
        # source so the frontend doesn't show a stuck file.
        cutoff = now - timedelta(hours=JOB_THRESHOLD_HOURS)

        async for session in acquire_org_async_session(self.organisation_id):
            rows = (
                await session.execute(
                    select(
                        col(KnowledgeProcessingJob.job_id),
                        col(KnowledgeProcessingJob.source_id),
                    ).where(
                        col(KnowledgeProcessingJob.status).in_(
                            (JobStatus.PENDING, JobStatus.IN_PROGRESS)
                        ),
                        col(KnowledgeProcessingJob.created_at) < cutoff,
                    )
                )
            ).all()

            if not rows:
                continue

            job_ids = [r[0] for r in rows]
            source_ids = list({r[1] for r in rows})
            error = f"Stuck beyond {JOB_THRESHOLD_HOURS}h (cleanup at {now.isoformat()})"

            # Fail the stuck jobs
            await session.execute(
                update(KnowledgeProcessingJob)
                .where(col(KnowledgeProcessingJob.job_id).in_(job_ids))
                .values(status=JobStatus.FAILED, error_message=error)
            )
            # Also sync the parent source — this is why it's called "safety net"
            await session.execute(
                update(OrganisationSource)
                .where(
                    col(OrganisationSource.source_id).in_(source_ids),
                    col(OrganisationSource.status).in_(ACTIVE_SOURCE_STATUSES),
                )
                .values(status=OrganisationSourceStatus.FAILED, error_message=error)
            )
            await session.commit()
            counts["stale_jobs"] = len(job_ids)

            logger.info(
                "Safety net: failed %d stale jobs across %d sources for org=%s",
                len(job_ids),
                len(source_ids),
                self.organisation_id,
            )

        logger.info(
            "Cleanup done for org=%s: stale_sources=%d, orphaned=%d, stale_jobs=%d",
            self.organisation_id,
            counts["stale_sources"],
            counts["orphaned_sources"],
            counts["stale_jobs"],
        )

        return {"organisation_id": str(self.organisation_id), **counts}
