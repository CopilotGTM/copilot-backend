"""Initial backfill claim service for deal-mapping workers."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_eligibility import (
    DealMappingEligibilityService,
)
from src.workers.deal_worker.utils.pipeline_dispatch import AUTOMATED_PIPELINE_USER_ID

INITIAL_BACKFILL_MAPPING_PURPOSE = "initial_backfill"
INITIAL_BACKFILL_SOURCE = "deal_mapping_initial_backfill"
INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT = 1000
INITIAL_BACKFILL_STALE_PENDING_MINUTES_ENV = "DEAL_MAPPING_INITIAL_BACKFILL_STALE_PENDING_MINUTES"
INITIAL_BACKFILL_STALE_IN_PROGRESS_MINUTES_ENV = (
    "DEAL_MAPPING_INITIAL_BACKFILL_STALE_IN_PROGRESS_MINUTES"
)
DEFAULT_STALE_PENDING_MINUTES = 5
DEFAULT_STALE_IN_PROGRESS_MINUTES = 60

logger = logging.getLogger(__name__)


class DealMappingBackfillClaimService:
    """Claims durable per-deal mapping jobs for scheduled initial backfill."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id
        self._eligibility = DealMappingEligibilityService(organisation_id)

    async def claim_stakeholder_backfill_batch(self, *, limit: int) -> dict[str, Any]:
        """Claim the next eligible stakeholder-sync jobs for this organization."""
        started_at = datetime.now(UTC).isoformat()
        claim_batch_id = uuid4()
        deal_tasks: list[dict[str, str]] = []

        async for session in acquire_org_async_session(self.organisation_id):
            if not await self._try_acquire_claim_lock(session):
                return {
                    "status": "skipped",
                    "reason": "claim_lock_not_acquired",
                    "organisation_id": str(self.organisation_id),
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "eligible_deals": 0,
                    "candidate_deals_scanned": 0,
                    "candidate_scan_limit": INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT,
                    "candidate_scan_capped": False,
                    "already_active": 0,
                    "already_completed": 0,
                    "claimable_deals": 0,
                    "claimed_deals": 0,
                    "deal_tasks_dispatched": 0,
                    "claim_batch_id": str(claim_batch_id),
                    "deal_tasks": [],
                }

            active_deal_ids, completed_deal_ids = await self._load_existing_stakeholder_claims(
                session
            )
            eligibility_result = await self._eligibility.list_eligible_deal_ids_with_scan_metrics(
                exclude_deal_ids=active_deal_ids | completed_deal_ids,
                candidate_scan_limit=INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT,
            )
            if eligibility_result.candidate_scan_capped:
                logger.info(
                    "Initial deal-mapping backfill candidate scan capped",
                    extra={
                        "organisation_id": str(self.organisation_id),
                        "candidate_deals_scanned": eligibility_result.candidate_deals_scanned,
                        "candidate_scan_limit": eligibility_result.candidate_scan_limit,
                    },
                )
            eligible_deal_ids = eligibility_result.eligible_deal_ids
            claimable_deal_ids = [
                deal_id
                for deal_id in eligible_deal_ids
                if deal_id not in active_deal_ids and deal_id not in completed_deal_ids
            ]
            claimed_deal_ids = claimable_deal_ids[:limit]

            if claimed_deal_ids:
                for deal_id in claimed_deal_ids:
                    job = DealJobs(
                        organisation_id=self.organisation_id,
                        user_id=AUTOMATED_PIPELINE_USER_ID,
                        job_type=DealJobType.STAKEHOLDER_SYNC,
                        deal_id=deal_id,
                        event_id=None,
                        status=JobStatus.PENDING,
                        job_metadata={
                            "source": INITIAL_BACKFILL_SOURCE,
                            "mapping_purpose": INITIAL_BACKFILL_MAPPING_PURPOSE,
                            "claim_batch_id": str(claim_batch_id),
                            "deal_id": str(deal_id),
                        },
                    )
                    session.add(job)
                    deal_tasks.append(
                        {
                            "organisation_id": str(self.organisation_id),
                            "deal_id": str(deal_id),
                            "job_id": str(job.job_id),
                        }
                    )
                await session.commit()

            return {
                "status": "completed",
                "organisation_id": str(self.organisation_id),
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "eligible_deals": len(eligible_deal_ids),
                "candidate_deals_scanned": eligibility_result.candidate_deals_scanned,
                "candidate_scan_limit": eligibility_result.candidate_scan_limit,
                "candidate_scan_capped": eligibility_result.candidate_scan_capped,
                "already_active": len(active_deal_ids),
                "already_completed": len(completed_deal_ids),
                "claimable_deals": len(claimable_deal_ids),
                "claimed_deals": len(claimed_deal_ids),
                "deal_tasks_dispatched": 0,
                "claim_batch_id": str(claim_batch_id),
                "deal_tasks": deal_tasks,
            }

        return {
            "status": "failed",
            "reason": "database_session_unavailable",
            "organisation_id": str(self.organisation_id),
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "eligible_deals": 0,
            "candidate_deals_scanned": 0,
            "candidate_scan_limit": INITIAL_BACKFILL_CANDIDATE_SCAN_LIMIT,
            "candidate_scan_capped": False,
            "already_active": 0,
            "already_completed": 0,
            "claimable_deals": 0,
            "claimed_deals": 0,
            "deal_tasks_dispatched": 0,
            "claim_batch_id": str(claim_batch_id),
            "deal_tasks": [],
        }

    async def _try_acquire_claim_lock(self, session) -> bool:
        lock_key = f"deal_mapping_initial_backfill_claim:{self.organisation_id}"
        result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": lock_key},
        )
        return bool(result.scalar())

    async def _load_existing_stakeholder_claims(self, session) -> tuple[set[UUID], set[UUID]]:
        active_deal_ids: set[UUID] = set()
        completed_deal_ids: set[UUID] = set()
        stale_jobs_found = False
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.job_type) == DealJobType.STAKEHOLDER_SYNC,
            col(DealJobs.status).in_(
                [JobStatus.PENDING, JobStatus.IN_PROGRESS, JobStatus.COMPLETED]
            ),
        )
        result = await session.execute(stmt)
        for job in result.scalars().all():
            if not job.deal_id:
                continue
            if (job.job_metadata or {}).get("mapping_purpose") != (
                INITIAL_BACKFILL_MAPPING_PURPOSE
            ):
                continue
            if job.status in {JobStatus.PENDING, JobStatus.IN_PROGRESS}:
                if self._is_stale_active_job(job):
                    job.status = JobStatus.FAILED
                    job.error_message = "Stale initial backfill job replaced"
                    job.job_metadata = {
                        **(job.job_metadata or {}),
                        "stale_replaced": True,
                        "stale_replaced_at": datetime.now(UTC).isoformat(),
                    }
                    stale_jobs_found = True
                    logger.info(
                        "Stale initial-backfill stakeholder job replaced",
                        extra={
                            "organisation_id": str(self.organisation_id),
                            "deal_id": str(job.deal_id),
                            "job_id": str(job.job_id),
                            "job_status": job.status.value,
                        },
                    )
                    continue
                active_deal_ids.add(job.deal_id)
            elif job.status == JobStatus.COMPLETED:
                completed_deal_ids.add(job.deal_id)
        if stale_jobs_found:
            await session.commit()
        return active_deal_ids, completed_deal_ids

    async def mark_dispatch_failed(self, *, job_id: UUID, error: str) -> None:
        """Release a claimed initial-backfill job when Celery dispatch fails."""
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(DealJobs).where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.job_id) == job_id,
                col(DealJobs.job_type) == DealJobType.STAKEHOLDER_SYNC,
            )
            result = await session.execute(stmt)
            job = result.scalars().first()
            if job is None:
                return
            if (job.job_metadata or {}).get("mapping_purpose") != INITIAL_BACKFILL_MAPPING_PURPOSE:
                return

            job.status = JobStatus.FAILED
            job.error_message = f"Initial backfill dispatch failed: {error}"
            job.job_metadata = {
                **(job.job_metadata or {}),
                "dispatch_status": "failed",
                "dispatch_error": error,
                "dispatch_failed_at": datetime.now(UTC).isoformat(),
            }
            session.add(job)
            await session.commit()
            logger.warning(
                "Initial backfill dispatch failed after claim",
                extra={
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(job.deal_id) if job.deal_id else None,
                    "job_id": str(job.job_id),
                },
            )
            return

    @staticmethod
    def _is_stale_active_job(job: DealJobs) -> bool:
        now = datetime.now(UTC)
        created_at = DealMappingBackfillClaimService._as_aware_utc(job.created_at)
        updated_at = DealMappingBackfillClaimService._as_aware_utc(job.updated_at)
        if job.status == JobStatus.PENDING:
            threshold = _threshold_from_env(
                INITIAL_BACKFILL_STALE_PENDING_MINUTES_ENV,
                DEFAULT_STALE_PENDING_MINUTES,
            )
            return created_at is not None and now - created_at > threshold
        if job.status == JobStatus.IN_PROGRESS:
            reference = updated_at or created_at
            threshold = _threshold_from_env(
                INITIAL_BACKFILL_STALE_IN_PROGRESS_MINUTES_ENV,
                DEFAULT_STALE_IN_PROGRESS_MINUTES,
            )
            return reference is not None and now - reference > threshold
        return False

    @staticmethod
    def _as_aware_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _threshold_from_env(env_name: str, default_minutes: int) -> timedelta:
    raw_value = os.getenv(env_name)
    if raw_value is None:
        return timedelta(minutes=default_minutes)
    try:
        minutes = int(raw_value)
    except ValueError:
        return timedelta(minutes=default_minutes)
    if minutes <= 0:
        return timedelta(minutes=default_minutes)
    return timedelta(minutes=minutes)
