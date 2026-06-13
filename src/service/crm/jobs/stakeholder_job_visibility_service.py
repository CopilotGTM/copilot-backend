"""Orchestrates user-visible stakeholder-triggered deal jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.client import send_deal_task_chain_async
from src.workers.deal_worker.services.deal_change_detector import StakeholderChangeType

logger = get_logger(__name__)


TaskKwargsBuilder = Callable[[str, str, str], dict]


@dataclass(frozen=True)
class StakeholderProcessingStep:
    """One ordered step in the deterministic stakeholder processing pipeline."""

    job_type: DealJobType
    task_name: str
    build_kwargs: TaskKwargsBuilder


def _deal_rematch_kwargs(org_id: str, deal_id: str, job_id: str) -> dict:
    return {"organisation_id": org_id, "deal_id": deal_id, "job_id": job_id}


def _stakeholder_sync_kwargs(org_id: str, deal_id: str, job_id: str) -> dict:
    return {
        "organisation_id": org_id,
        "deal_id": deal_id,
        "change_type": StakeholderChangeType.MANUAL_REFRESH.value,
        "integration_id": None,
        "integration_type": None,
        "trigger_rematching": False,
        "job_id": job_id,
    }


STAKEHOLDER_PROCESSING_PIPELINE = (
    StakeholderProcessingStep(
        job_type=DealJobType.DEAL_EMAIL_REMATCH,
        task_name="deal.match_email_to_deals",
        build_kwargs=_deal_rematch_kwargs,
    ),
    StakeholderProcessingStep(
        job_type=DealJobType.DEAL_CALENDAR_REMATCH,
        task_name="deal.match_calendar_to_deals",
        build_kwargs=_deal_rematch_kwargs,
    ),
    StakeholderProcessingStep(
        job_type=DealJobType.STAKEHOLDER_SYNC,
        task_name="stakeholder.sync_deal_stakeholders",
        build_kwargs=_stakeholder_sync_kwargs,
    ),
)


class StakeholderJobVisibilityService:
    """Create and dispatch user-visible processing jobs for one stakeholder action.

    The service guarantees a single operation group for the full set of jobs
    and keeps DB writes aligned with chain dispatch so the UI can safely
    display job progress without orphan rows.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        """Bind tenant context and async DB session for this request."""
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def create_stakeholder_processing_jobs(
        self,
        deal_id: UUID,
        stakeholder_id: UUID,
    ) -> list[DealJobs]:
        """Create and enqueue deterministic post-create stakeholder jobs.

        The three jobs are created in one transaction so:
        - they are discoverable in a single operation group for reconnect/retry UX
        - each task receives a stable job_id for progress/SSE
        - workers can read committed job rows before publishing progress
        - Celery runs the processing work in the order defined by the pipeline
        """
        operation_group_id = str(uuid4())
        jobs = [
            self._build_job(
                step.job_type,
                deal_id,
                stakeholder_id,
                operation_group_id,
            )
            for step in STAKEHOLDER_PROCESSING_PIPELINE
        ]

        for job in jobs:
            self.session.add(job)

        await self.session.flush()
        for job in jobs:
            await self.session.refresh(job)
        await self.session.commit()

        try:
            await send_deal_task_chain_async(
                self._build_task_chain(jobs, deal_id),
                queue="deal",
            )
        except Exception:
            await self._mark_jobs_failed(jobs, "Failed to enqueue stakeholder processing chain")
            raise

        logger.info(
            "Created stakeholder processing jobs org=%s deal=%s stakeholder=%s jobs=%s",
            self.organisation_id,
            deal_id,
            stakeholder_id,
            [str(job.job_id) for job in jobs],
        )
        return jobs

    def _build_job(
        self,
        job_type: DealJobType,
        deal_id: UUID,
        stakeholder_id: UUID,
        operation_group_id: str,
    ) -> DealJobs:
        """Build a visible job row carrying stakeholder-specific operation metadata.

        operation_group_id lets the UI group the three jobs from one manual add
        action, while trigger/stakeholder_id preserve stable filters for status
        views and reconnect screens.
        """
        return DealJobs(
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            job_type=job_type,
            deal_id=deal_id,
            event_id=None,
            stakeholder_id=stakeholder_id,
            status=JobStatus.PENDING,
            job_metadata={
                "trigger": "stakeholder_created",
                "operation_group_id": operation_group_id,
                "stakeholder_id": str(stakeholder_id),
                "participant_id": str(stakeholder_id),
                "deal_id": str(deal_id),
            },
        )

    def _build_task_chain(self, jobs: list[DealJobs], deal_id: UUID) -> list[dict]:
        """Build the Celery chain payload in the product-required order."""
        job_by_type = {job.job_type: job for job in jobs}
        return [
            self._build_task_signature(step, job_by_type[step.job_type], deal_id)
            for step in STAKEHOLDER_PROCESSING_PIPELINE
        ]

    def _build_task_signature(
        self,
        step: StakeholderProcessingStep,
        job: DealJobs,
        deal_id: UUID,
    ) -> dict:
        """Build one immutable Celery signature payload for the ordered chain."""
        org_id = str(self.organisation_id)
        deal_id_str = str(deal_id)
        job_id = str(job.job_id)

        return {
            "task_name": step.task_name,
            "kwargs": step.build_kwargs(org_id, deal_id_str, job_id),
        }

    async def _mark_jobs_failed(self, jobs: list[DealJobs], message: str) -> None:
        for job in jobs:
            job.status = JobStatus.FAILED
            job.error_message = message
        await self.session.commit()
