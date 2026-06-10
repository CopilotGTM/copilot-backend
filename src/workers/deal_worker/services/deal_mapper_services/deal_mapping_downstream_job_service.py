"""Creates downstream rematch jobs for deal mapping."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_backfill_claim_service import (
    INITIAL_BACKFILL_MAPPING_PURPOSE,
    INITIAL_BACKFILL_SOURCE,
)
from src.workers.deal_worker.utils.pipeline_dispatch import AUTOMATED_PIPELINE_USER_ID

EVENT_REMATCH_MAPPING_PURPOSE = "event_rematch"
EVENT_REMATCH_SOURCE = "deal_mapping_event_rematch"


class DealMappingDownstreamJobService:
    """Claims calendar/email rematch jobs created after stakeholder sync."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    async def claim_rematch_jobs(
        self,
        *,
        deal_id: UUID,
        parent_stakeholder_job_id: UUID | None,
        change_type: str,
    ) -> dict[str, Any]:
        """Create missing downstream rematch jobs for a stakeholder-sync result."""
        target_types = {
            DealJobType.DEAL_CALENDAR_REMATCH,
            DealJobType.DEAL_EMAIL_REMATCH,
        }
        mapping_purpose = self._mapping_purpose(change_type)
        source = self._source(change_type)
        reusable_statuses = self._reusable_statuses(change_type)
        result: dict[str, Any] = {
            "calendar_job_id": None,
            "email_job_id": None,
            "calendar_dispatch_required": False,
            "email_dispatch_required": False,
        }

        async for session in acquire_org_async_session(self.organisation_id):
            existing_jobs = await self._load_existing_jobs(
                session,
                deal_id,
                target_types,
                mapping_purpose,
                reusable_statuses,
            )
            jobs_to_create: list[DealJobs] = []

            for job_type in (
                DealJobType.DEAL_CALENDAR_REMATCH,
                DealJobType.DEAL_EMAIL_REMATCH,
            ):
                result_prefix = (
                    "calendar" if job_type == DealJobType.DEAL_CALENDAR_REMATCH else "email"
                )
                existing_job = existing_jobs.get(job_type)
                if existing_job:
                    result[f"{result_prefix}_job_id"] = str(existing_job.job_id)
                    continue

                job = DealJobs(
                    organisation_id=self.organisation_id,
                    user_id=AUTOMATED_PIPELINE_USER_ID,
                    job_type=job_type,
                    deal_id=deal_id,
                    event_id=None,
                    status=JobStatus.PENDING,
                    job_metadata={
                        "source": source,
                        "mapping_purpose": mapping_purpose,
                        "deal_id": str(deal_id),
                        "parent_stakeholder_job_id": (
                            str(parent_stakeholder_job_id) if parent_stakeholder_job_id else None
                        ),
                        "created_from_change_type": change_type,
                    },
                )
                jobs_to_create.append(job)
                result[f"{result_prefix}_job_id"] = str(job.job_id)
                result[f"{result_prefix}_dispatch_required"] = True

            if jobs_to_create:
                for job in jobs_to_create:
                    session.add(job)
                await session.commit()
            break

        return result

    async def claim_initial_backfill_rematch_jobs(
        self,
        *,
        deal_id: UUID,
        parent_stakeholder_job_id: UUID | None,
        change_type: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper for older callers."""
        return await self.claim_rematch_jobs(
            deal_id=deal_id,
            parent_stakeholder_job_id=parent_stakeholder_job_id,
            change_type=change_type,
        )

    async def _load_existing_jobs(
        self,
        session,
        deal_id: UUID,
        target_types: set[DealJobType],
        mapping_purpose: str,
        reusable_statuses: set[JobStatus],
    ) -> dict[DealJobType, DealJobs]:
        existing_jobs: dict[DealJobType, DealJobs] = {}
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id) == deal_id,
            col(DealJobs.job_type).in_(target_types),
            col(DealJobs.status).in_(list(reusable_statuses)),
        )
        result = await session.execute(stmt)
        for job in result.scalars().all():
            if job.status not in reusable_statuses:
                continue
            if (job.job_metadata or {}).get("mapping_purpose") != mapping_purpose:
                continue
            existing_jobs[job.job_type] = job
        return existing_jobs

    def _mapping_purpose(self, change_type: str) -> str:
        if change_type == "INITIAL_BACKFILL":
            return INITIAL_BACKFILL_MAPPING_PURPOSE
        return EVENT_REMATCH_MAPPING_PURPOSE

    def _source(self, change_type: str) -> str:
        if change_type == "INITIAL_BACKFILL":
            return INITIAL_BACKFILL_SOURCE
        return EVENT_REMATCH_SOURCE

    def _reusable_statuses(self, change_type: str) -> set[JobStatus]:
        if change_type == "INITIAL_BACKFILL":
            return {JobStatus.PENDING, JobStatus.IN_PROGRESS, JobStatus.COMPLETED}
        return {JobStatus.PENDING, JobStatus.IN_PROGRESS}
