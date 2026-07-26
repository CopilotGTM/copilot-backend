"""Persist, publish, and recover user-visible deal-level jobs.

Deal and stakeholder API/tool entry points use this service for work not owned
by one meeting event. ``DealMeetingJobService`` handles event-scoped job
requests; both services share ``DealJobs`` for durable status and SSE reconnection.

Stakeholder and communication-hydration jobs commit their visible row before
queue publication so fast workers can always load it. Their enqueue metadata
records whether the broker handoff was confirmed. Stakeholder reads may
republish an unconfirmed group, so recovery is intentionally at-least-once and
depends on downstream idempotency.
"""

from __future__ import annotations

from uuid import UUID, uuid4

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
from src.workers.deal_worker.utils.contact_communication_hydration_dispatch import (
    dispatch_contact_communication_hydration_async,
)

logger = get_logger(__name__)

# Celery registration used for all generic deal-level analysis jobs.
DEAL_SUMMARY_TASK_NAME = "deal_summary_task.process"

# Terminal jobs are replayed by the SSE service, not advertised as reconnectable work.
ACTIVE_JOB_STATUSES = {JobStatus.PENDING, JobStatus.IN_PROGRESS}

# Eligible stakeholder mutations surface one visible job whose normal replay path
# continues into targeted hydration before terminal state.
STAKEHOLDER_PROCESSING_JOB_TYPES = {
    DealJobType.STAKEHOLDER_SYNC,
}

# Recovery preserves this order when the visible operation grows beyond one task.
STAKEHOLDER_PROCESSING_ORDER = (DealJobType.STAKEHOLDER_SYNC,)

# Each visible job type must map to the exact registered Celery task name.
STAKEHOLDER_PROCESSING_TASK_NAMES = {
    DealJobType.STAKEHOLDER_SYNC: "stakeholder.sync_deal_stakeholders",
}

# These types are allowed through ``create_job`` and always persist with event_id=None.
DEAL_LEVEL_JOB_TYPES = {
    DealJobType.DEAL_SUMMARY,
    DealJobType.DEAL_RISK_ANALYSIS,
    DealJobType.DEAL_NEXT_STEPS,
    DealJobType.STAKEHOLDER_ANALYSIS,
}


def _enum_value(value) -> str:
    """Serialize SQLModel enum fields whether loaded as enums or raw strings."""
    return value.value if hasattr(value, "value") else str(value)


def _job_response(job: DealJobs) -> dict:
    """Return the stable subset embedded in active-job API responses."""
    return {
        "job_id": job.job_id,
        "job_type": _enum_value(job.job_type),
        "status": _enum_value(job.status),
    }


def _metadata(job: DealJobs) -> dict:
    """Treat legacy or malformed non-dict metadata as empty recovery state."""
    value = job.job_metadata
    return value if isinstance(value, dict) else {}


def _is_unenqueued_stakeholder_group_job(job: DealJobs) -> bool:
    """Identify visible stakeholder work whose broker handoff is unconfirmed."""
    metadata = _metadata(job)
    return (
        job.job_type in STAKEHOLDER_PROCESSING_JOB_TYPES
        and metadata.get("trigger") == "stakeholder_created"
        and bool(metadata.get("operation_group_id"))
        and metadata.get("enqueue_status") != "enqueued"
    )


def _stakeholder_task_kwargs(job: DealJobs) -> dict:
    """Rebuild the deterministic task payload stored indirectly by a job row."""
    if not job.deal_id:
        raise ValueError("Stakeholder processing job is missing deal_id")

    org_id = str(job.organisation_id)
    deal_id = str(job.deal_id)
    job_id = str(job.job_id)
    return {
        "organisation_id": org_id,
        "deal_id": deal_id,
        "change_type": "MANUAL_REFRESH",
        "integration_id": None,
        "integration_type": None,
        "trigger_rematching": True,
        "job_id": job_id,
        "emit_events": True,
    }


class DealJobServiceError(Exception):
    """Carry an HTTP-safe message and status from the service to API callers."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealJobService:
    """Create and query non-event jobs in one organisation.

    Deal-level jobs are operations on the entire deal, not tied to a specific meeting.
    New rows belong to ``user_id`` for SSE authorization. Deal/contact lookups
    retain the organisation boundary; user-private contact research additionally
    filters by the requesting user.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        """Bind the tenant write session and owner of newly created visible jobs."""
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self) -> None:
        """Leave cleanup to the FastAPI dependency that owns the shared session."""
        # This service does not allocate a second connection or external client.
        pass

    async def create_stakeholder_processing_jobs(
        self,
        deal_id: UUID,
        stakeholder_id: UUID | None = None,
        *,
        participant_ids: list[UUID] | None = None,
    ) -> list[DealJobs]:
        """Create and publish one visible stakeholder-normalization operation.

        On eligible rematching paths the stakeholder worker continues into
        targeted hydration; otherwise it owns terminal SSE state itself. The job
        row commits before publication so the worker can load it.
        ``enqueue_status`` makes that non-atomic boundary observable. Recovery
        may republish work that reached Celery but crashed before its confirmation
        commit, so the worker must remain idempotent.
        """
        # Preserve caller order while removing duplicate participant IDs from metadata.
        affected_participant_ids = list(
            dict.fromkeys(participant_ids or ([stakeholder_id] if stakeholder_id else []))
        )
        if not affected_participant_ids:
            raise ValueError("At least one participant ID is required")
        deal_level_action = participant_ids is not None
        operation_group_id = str(uuid4())
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            job_type=DealJobType.STAKEHOLDER_SYNC,
            deal_id=deal_id,
            event_id=None,
            stakeholder_id=None if deal_level_action else stakeholder_id,
            status=JobStatus.PENDING,
            job_metadata={
                "trigger": "stakeholder_created",
                "enqueue_status": "pending",
                "operation_group_id": operation_group_id,
                **(
                    {"participant_ids": [str(value) for value in affected_participant_ids]}
                    if deal_level_action
                    else {
                        "stakeholder_id": str(stakeholder_id),
                        "participant_id": str(stakeholder_id),
                    }
                ),
                "deal_id": str(deal_id),
            },
        )
        self.session.add(job)
        await self.session.flush()
        await self.session.refresh(job)
        # Commit before publishing so a fast worker can always load the job it reports.
        # Until the later "enqueued" commit, recovery treats publication as unconfirmed.
        await self.session.commit()

        try:
            celery_chain_id = await send_deal_task_chain_async(
                [
                    {
                        "task_name": STAKEHOLDER_PROCESSING_TASK_NAMES[job.job_type],
                        "kwargs": _stakeholder_task_kwargs(job),
                    }
                ],
                queue="deal",
            )
        except Exception:
            # Persist the unconfirmed handoff rather than leaving an unexplained PENDING
            # row. Broker timeouts can be ambiguous, so recovery remains at-least-once.
            job.error_message = "Failed to enqueue stakeholder processing chain"
            job.job_metadata = {**_metadata(job), "enqueue_status": "failed"}
            await self.session.commit()
            raise

        # Confirm publication only after the broker returns its chain ID.
        job.job_metadata = {
            **_metadata(job),
            "enqueue_status": "enqueued",
            "celery_chain_id": celery_chain_id,
        }
        await self.session.commit()
        return [job]

    async def create_communication_hydration_job(
        self,
        deal_id: UUID,
        *,
        trigger: str = "deal_created",
    ) -> DealJobs:
        """Create a visible job for direct deal-scoped communication hydration.

        The row commits before dispatch for worker visibility. Unlike deterministic
        stakeholder recovery, a dispatch exception marks this job terminally failed
        so clients do not reconnect to work that was never published.
        """
        job = DealJobs(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            job_type=DealJobType.DEAL_COMMUNICATION_HYDRATION,
            deal_id=deal_id,
            event_id=None,
            status=JobStatus.PENDING,
            job_metadata={"trigger": trigger, "enqueue_status": "pending"},
        )
        self.session.add(job)
        await self.session.flush()
        await self.session.refresh(job)
        # Publish only after the job is durable; hydration may start immediately.
        await self.session.commit()

        try:
            task_ids = await dispatch_contact_communication_hydration_async(
                organisation_id=self.organisation_id,
                deal_ids=[deal_id],
                trigger=trigger,
                job_id=job.job_id,
                job_type=DealJobType.DEAL_COMMUNICATION_HYDRATION,
                emit_events=True,
            )
        except Exception:
            # No worker owns this job after a failed handoff, so expose a terminal
            # failure rather than an indefinitely active row.
            job.status = JobStatus.FAILED
            job.error_message = "Failed to enqueue communication hydration"
            job.job_metadata = {**_metadata(job), "enqueue_status": "failed"}
            await self.session.commit()
            raise

        # Record every chunk/task ID returned by the bounded hydration dispatcher.
        job.job_metadata = {
            **_metadata(job),
            "enqueue_status": "enqueued",
            "celery_task_ids": task_ids,
        }
        await self.session.commit()
        return job

    async def create_job(
        self,
        job_type: DealJobType,
        deal_id: UUID,
        *,
        metadata: dict | None = None,
    ) -> DealJobs:
        """Create a generic deal-level analysis job and enqueue its worker.

        This lower-level method does not deduplicate; convenience methods such as
        ``create_deal_summary_job`` perform active-job reuse first. Queue
        publication precedes commit so a broker failure can roll back the row.
        A commit failure after successful publication has no recovery record here.

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

        # Use the async publisher so a broker round trip does not block the API loop.
        # A publish failure rolls back both the ID and pending job state.
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
        """Return an active summary job or create one for the whole deal.

        Analyzes all meeting transcripts for the deal to generate a comprehensive summary.
        Reusing active work keeps retries and browser reconnects on one SSE stream.

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
        """Return active deal-level work for idempotency and reconnection.

        The organisation predicate is the tenant boundary for the caller-supplied
        deal ID. ``event_id IS NULL`` prevents meeting jobs from satisfying a
        deal-level lookup.

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
        """Recover and return visible stakeholder work confirmed as enqueued.

        Unconfirmed queue handoffs are republished from deterministic job
        metadata. They may already have reached Celery, so replay is at-least-once.
        Incomplete groups stay hidden because this service cannot reconstruct them.
        """
        # The organisation predicate is the tenant boundary for the requested deal.
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
        # Recovery may mutate enqueue metadata before the response is filtered.
        jobs = await self._recover_unenqueued_stakeholder_processing_groups(jobs)

        # Never advertise an unconfirmed broker handoff as active user-visible work.
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
        """Republish complete deterministic groups whose enqueue is unconfirmed."""
        groups: dict[str, list[DealJobs]] = {}
        for job in jobs:
            if _is_unenqueued_stakeholder_group_job(job):
                groups.setdefault(str(_metadata(job)["operation_group_id"]), []).append(job)

        for operation_group_id, group_jobs in groups.items():
            # A partial group cannot be replayed safely because missing job rows also
            # mean missing task IDs and visible terminal-state ownership.
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

            # Rebuild the chain in its declared order; do not trust database row order.
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
                # Persist the unconfirmed result but keep the group eligible for a
                # future at-least-once recovery attempt.
                for job in ordered_jobs:
                    job.error_message = "Failed to enqueue stakeholder processing chain"
                    job.job_metadata = {**_metadata(job), "enqueue_status": "failed"}
                await self.session.commit()
                logger.exception(
                    "Failed to recover unenqueued stakeholder processing group org=%s group=%s",
                    self.organisation_id,
                    operation_group_id,
                )
                continue

            # Mark the handoff confirmed only after Celery returns a chain ID.
            for job in ordered_jobs:
                job.error_message = None
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

    async def get_active_contact_research_job(
        self,
        contact_id: UUID,
    ) -> dict | None:
        """Return this user's newest active research job for one contact.

        Contact identity lives in JSON metadata, so the query first applies
        organisation and user boundaries, then filters those trusted rows in memory.
        """
        stmt = (
            select(DealJobs)
            .where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.user_id) == self.user_id,
                col(DealJobs.job_type) == DealJobType.CONTACT_RESEARCH,
                col(DealJobs.status).in_(list(ACTIVE_JOB_STATUSES)),
            )
            .order_by(col(DealJobs.created_at).desc())
        )
        result = await self.session.execute(stmt)
        job = next(
            (
                candidate
                for candidate in result.scalars().all()
                if (candidate.job_metadata or {}).get("contact_id") == str(contact_id)
            ),
            None,
        )
        if not job:
            return None
        return _job_response(job)

    async def get_active_deal_jobs_status(self, deal_id: UUID) -> dict | None:
        """Build the active-job payload used for deal-page SSE reconnection.

        Returns a dict suitable for DealSummaryJobsStatus schema, or None if no active jobs.
        Used by deals_handler to include active job info for SSE reconnection.

        Args:
            deal_id: The deal to check for active jobs

        Returns:
            Dict with job info (job_id, job_type, status) keyed by job type, or None
        """
        active_summary_job = await self.get_active_job(deal_id, DealJobType.DEAL_SUMMARY)
        active_hydration_job = await self.get_active_job(
            deal_id,
            DealJobType.DEAL_COMMUNICATION_HYDRATION,
        )
        stakeholder_processing = await self.get_active_stakeholder_processing_jobs(deal_id)

        if not active_summary_job and not active_hydration_job and not stakeholder_processing:
            logger.debug("No active deal jobs found for deal %s", deal_id)
            return None

        response: dict = {}

        # A persisted job without its generated primary key cannot form a valid SSE URL.
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

        if active_hydration_job:
            response["communication_hydration"] = _job_response(active_hydration_job)

        if stakeholder_processing:
            response["stakeholder_processing"] = stakeholder_processing

        return response or None

    async def get_job(self, job_id: UUID) -> DealJobs | None:
        """Load a tenant-local job by primary key; callers enforce user ownership."""
        return await self.session.get(DealJobs, job_id)
