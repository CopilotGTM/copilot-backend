"""Service for managing deal-level jobs (deal summary, stakeholder analysis, etc.).

This service handles deal-level async operations that are NOT tied to a specific meeting/event.
For meeting-level jobs (briefing, meeting summary, followup), use DealMeetingJobService.

Both services share the same DealJobs table for job storage and tracking.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.client import send_deal_task_async, send_deal_task_chain_async

logger = get_logger(__name__)

# Task name for deal summary processing (dedicated task for deal-level jobs)
DEAL_SUMMARY_TASK_NAME = "deal_summary_task.process"

# Active jobs are the only jobs clients should reconnect to.
ACTIVE_JOB_STATUSES = {JobStatus.PENDING, JobStatus.IN_PROGRESS}

# Deterministic post-stakeholder-create operations surfaced as deal-level activity.
STAKEHOLDER_PROCESSING_JOB_TYPES = {
    DealJobType.DEAL_EMAIL_REMATCH,
    DealJobType.DEAL_CALENDAR_REMATCH,
    DealJobType.STAKEHOLDER_SYNC,
}

STAKEHOLDER_PROCESSING_ORDER = (
    DealJobType.DEAL_EMAIL_REMATCH,
    DealJobType.DEAL_CALENDAR_REMATCH,
    DealJobType.STAKEHOLDER_SYNC,
)

STAKEHOLDER_PROCESSING_TASK_NAMES = {
    DealJobType.DEAL_EMAIL_REMATCH: "deal.match_email_to_deals",
    DealJobType.DEAL_CALENDAR_REMATCH: "deal.match_calendar_to_deals",
    DealJobType.STAKEHOLDER_SYNC: "stakeholder.sync_deal_stakeholders",
}

# Deal-level job types (not tied to a specific event)
DEAL_LEVEL_JOB_TYPES = {
    DealJobType.DEAL_SUMMARY,
    DealJobType.DEAL_RISK_ANALYSIS,
    DealJobType.DEAL_NEXT_STEPS,
    DealJobType.STAKEHOLDER_ANALYSIS,
}


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _job_response(job: DealJobs) -> dict:
    return {
        "job_id": job.job_id,
        "job_type": _enum_value(job.job_type),
        "status": _enum_value(job.status),
    }


def _metadata(job: DealJobs) -> dict:
    value = job.job_metadata
    return value if isinstance(value, dict) else {}


def _is_unenqueued_stakeholder_group_job(job: DealJobs) -> bool:
    metadata = _metadata(job)
    return (
        job.job_type in STAKEHOLDER_PROCESSING_JOB_TYPES
        and metadata.get("trigger") == "stakeholder_created"
        and bool(metadata.get("operation_group_id"))
        and metadata.get("enqueue_status") != "enqueued"
    )


def _stakeholder_task_kwargs(job: DealJobs) -> dict:
    if not job.deal_id:
        raise ValueError("Stakeholder processing job is missing deal_id")

    org_id = str(job.organisation_id)
    deal_id = str(job.deal_id)
    job_id = str(job.job_id)
    if job.job_type == DealJobType.STAKEHOLDER_SYNC:
        return {
            "organisation_id": org_id,
            "deal_id": deal_id,
            "change_type": "MANUAL_REFRESH",
            "integration_id": None,
            "integration_type": None,
            "trigger_rematching": False,
            "job_id": job_id,
        }
    return {"organisation_id": org_id, "deal_id": deal_id, "job_id": job_id}


class DealJobServiceError(Exception):
    """Custom exception for deal job service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealJobService:
    """
    Service for creating and managing deal-level jobs.

    Deal-level jobs are operations on the entire deal, not tied to a specific meeting.
    Examples: Deal Summary, Stakeholder Analysis, Deal Risk Analysis.

    Uses the same DealJobs table but with event_id=None.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self) -> None:
        """Clean up resources (placeholder for future async cleanup)."""
        # Session lifecycle is managed by the caller/dependency
        pass

    async def create_job(
        self,
        job_type: DealJobType,
        deal_id: UUID,
        *,
        metadata: dict | None = None,
    ) -> DealJobs:
        """
        Create a new deal-level job and enqueue the task.

        Args:
            job_type: Type of deal-level job to create
            deal_id: The deal this job is for
            metadata: Optional job-specific metadata

        Returns:
            The created DealJobs for SSE subscription

        Raises:
            DealJobServiceError: If job type is not a deal-level job or task fails to enqueue
        """
        # Validate operation scope: only top-level deal jobs are allowed here;
        # meeting and event-level tasks must be created through the meeting job service.
        if job_type not in DEAL_LEVEL_JOB_TYPES:
            raise DealJobServiceError(
                f"{job_type.value} is not a deal-level job type. Use DealMeetingJobService for meeting jobs.",
                status_code=400,
            )

        # event_id=None marks this as a deal-scoped job, which keeps replay and
        # SSE reconnect semantics aligned with deal-level workflows.
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            job_type=job_type,
            deal_id=deal_id,
            event_id=None,  # Deal-level jobs have no event
            stakeholder_id=None,
            status=JobStatus.PENDING,
            job_metadata=metadata or {},
        )
        self.session.add(job)
        await self.session.flush()
        await self.session.refresh(job)

        # Enqueue Celery task (use async client to avoid blocking event loop)
        try:
            await send_deal_task_async(
                DEAL_SUMMARY_TASK_NAME,
                args=[str(self.organisation_id), str(job.job_id)],
                queue="deal_jobs",
                routing_key="deal_jobs",
            )
            logger.info(
                "Enqueued deal-level job org=%s job=%s type=%s deal=%s",
                self.organisation_id,
                job.job_id,
                job_type.value,
                deal_id,
            )
        except Exception as e:
            logger.error("Failed to enqueue deal job: %s", e, exc_info=True)
            await self.session.rollback()
            raise DealJobServiceError("Failed to schedule job.") from e

        await self.session.commit()
        return job

    async def create_deal_summary_job(self, deal_id: UUID) -> DealJobs:
        """
        Create a deal summary job.

        Analyzes all meeting transcripts for the deal to generate a comprehensive summary.

        Idempotent - returns existing job if one is already running.

        Args:
            deal_id: The deal to generate a summary for

        Returns:
            The created or existing DealJobs
        """
        job_type = DealJobType.DEAL_SUMMARY

        # Return the active job if present so callers reconnect instead of enqueuing
        # duplicate work.
        active_job = await self.get_active_job(deal_id, job_type)
        if active_job:
            logger.info(
                "Deal summary job already in progress for deal %s, returning existing job",
                deal_id,
            )
            return active_job

        return await self.create_job(
            job_type=job_type,
            deal_id=deal_id,
            metadata={"deal_id": str(deal_id)},
        )

    async def get_active_job(
        self,
        deal_id: UUID,
        job_type: DealJobType,
    ) -> DealJobs | None:
        """
        Get the currently running deal-level job for a specific type.

        Used for idempotency and job recovery when user reconnects.

        Args:
            deal_id: The deal to check
            job_type: The type of job to look for

        Returns:
            Active job if found, None otherwise
        """
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.job_type) == job_type,
            col(DealJobs.event_id).is_(None),  # Deal-level jobs have no event
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )

        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_active_stakeholder_processing_jobs(self, deal_id: UUID) -> list[dict]:
        """Return active deterministic stakeholder post-create jobs for a deal."""
        stmt = (
            select(DealJobs)
            .where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.deal_id) == deal_id,
                col(DealJobs.job_type).in_(list(STAKEHOLDER_PROCESSING_JOB_TYPES)),
                col(DealJobs.event_id).is_(None),
                col(DealJobs.status).in_(list(ACTIVE_JOB_STATUSES)),
            )
            .order_by(col(DealJobs.created_at).asc())
        )
        result = await self.session.execute(stmt)
        jobs = result.scalars().all()
        jobs = await self._recover_unenqueued_stakeholder_processing_groups(jobs)

        return [
            _job_response(job)
            for job in jobs
            if job.job_type in STAKEHOLDER_PROCESSING_JOB_TYPES
            and job.status in ACTIVE_JOB_STATUSES
            and _metadata(job).get("enqueue_status", "enqueued") == "enqueued"
        ]

    async def _recover_unenqueued_stakeholder_processing_groups(
        self, jobs: list[DealJobs]
    ) -> list[DealJobs]:
        groups: dict[str, list[DealJobs]] = {}
        for job in jobs:
            if _is_unenqueued_stakeholder_group_job(job):
                groups.setdefault(str(_metadata(job)["operation_group_id"]), []).append(job)

        for operation_group_id, group_jobs in groups.items():
            if len(group_jobs) != len(STAKEHOLDER_PROCESSING_ORDER):
                logger.warning(
                    "Stakeholder processing group is incomplete; hiding from active jobs org=%s group=%s jobs=%s",
                    self.organisation_id,
                    operation_group_id,
                    [str(job.job_id) for job in group_jobs],
                )
                continue

            by_type = {job.job_type: job for job in group_jobs}
            if any(job_type not in by_type for job_type in STAKEHOLDER_PROCESSING_ORDER):
                continue

            ordered_jobs = [by_type[job_type] for job_type in STAKEHOLDER_PROCESSING_ORDER]
            try:
                celery_chain_id = await send_deal_task_chain_async(
                    [
                        {
                            "task_name": STAKEHOLDER_PROCESSING_TASK_NAMES[job.job_type],
                            "kwargs": _stakeholder_task_kwargs(job),
                        }
                        for job in ordered_jobs
                    ],
                    queue="deal",
                )
            except Exception:
                for job in ordered_jobs:
                    job.status = JobStatus.FAILED
                    job.error_message = "Failed to enqueue stakeholder processing chain"
                    job.job_metadata = {**_metadata(job), "enqueue_status": "failed"}
                await self.session.commit()
                logger.exception(
                    "Failed to recover unenqueued stakeholder processing group org=%s group=%s",
                    self.organisation_id,
                    operation_group_id,
                )
                continue

            for job in ordered_jobs:
                job.job_metadata = {
                    **_metadata(job),
                    "enqueue_status": "enqueued",
                    "celery_chain_id": celery_chain_id,
                    "enqueue_recovered": True,
                }
            await self.session.commit()
            logger.warning(
                "Recovered unenqueued stakeholder processing group org=%s group=%s jobs=%s",
                self.organisation_id,
                operation_group_id,
                [str(job.job_id) for job in ordered_jobs],
            )

        return jobs

    async def get_active_stakeholder_research_job(
        self,
        deal_id: UUID,
        stakeholder_id: UUID,
    ) -> dict | None:
        """Return the active manual stakeholder research job for one stakeholder."""
        stmt = (
            select(DealJobs)
            .where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.user_id) == self.user_id,
                col(DealJobs.deal_id) == deal_id,
                col(DealJobs.stakeholder_id) == stakeholder_id,
                col(DealJobs.job_type) == DealJobType.STAKEHOLDER_RESEARCH,
                col(DealJobs.status).in_(list(ACTIVE_JOB_STATUSES)),
            )
            .order_by(col(DealJobs.created_at).desc())
        )
        result = await self.session.execute(stmt)
        job = result.scalars().first()
        if not job:
            return None
        return _job_response(job)

    async def get_active_deal_jobs_status(self, deal_id: UUID) -> dict | None:
        """
        Get active deal-level jobs status for API response.

        Returns a dict suitable for DealSummaryJobsStatus schema, or None if no active jobs.
        Used by deals_handler to include active job info for SSE reconnection.

        Args:
            deal_id: The deal to check for active jobs

        Returns:
            Dict with job info (job_id, job_type, status) keyed by job type, or None
        """
        active_summary_job = await self.get_active_job(deal_id, DealJobType.DEAL_SUMMARY)
        stakeholder_processing = await self.get_active_stakeholder_processing_jobs(deal_id)

        if not active_summary_job and not stakeholder_processing:
            logger.debug("No active deal summary job found for deal %s", deal_id)
            return None

        response: dict = {}

        # Ensure job_id exists before returning
        if active_summary_job and not active_summary_job.job_id:
            logger.warning(
                "Active deal summary job found but job_id is None for deal %s",
                deal_id,
            )
        elif active_summary_job:
            logger.debug(
                "Active deal summary job found: deal=%s job_id=%s status=%s",
                deal_id,
                active_summary_job.job_id,
                active_summary_job.status,
            )
            response["deal_summary"] = _job_response(active_summary_job)

        if stakeholder_processing:
            response["stakeholder_processing"] = stakeholder_processing

        return response or None

    async def get_job(self, job_id: UUID) -> DealJobs | None:
        """Get a job by ID."""
        return await self.session.get(DealJobs, job_id)
