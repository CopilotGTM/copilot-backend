"""Account research dispatch boundary for API and contact-create callers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.infra.database.sessions import acquire_org_async_session
from src.service.crm.accounts.domain_resolution_service import AccountDomainResolutionService
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.workers.deal_worker.client import send_deal_task_async

logger = logging.getLogger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_RESEARCH_COOLDOWN_DAYS = 30
ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.IN_PROGRESS)


async def dispatch_account_research_async(
    *,
    organisation_id: UUID,
    company_id: UUID,
    user_id: UUID | None = None,
    trigger: str = "account_created",
    force_refresh: bool = False,
) -> str | None:
    """Create and enqueue an account research job when the account is eligible."""
    async for session in acquire_org_async_session(organisation_id):
        account = await session.get(CRMCompany, company_id)
        if not account or account.organisation_id != organisation_id or account.is_deleted:
            logger.warning(
                "Account research dispatch skipped because account was not found",
                extra={
                    "event": "account_research_dispatch_not_found",
                    "organisation_id": str(organisation_id),
                    "company_id": str(company_id),
                },
            )
            return None

        domain = _safe_domain(account.domain or account.website)
        linkedin_url = (account.linkedin_url or "").strip() or None
        if not domain and not linkedin_url:
            return None

        if not force_refresh and account.account_summary and _is_recent(account.last_researched_at):
            return None

        existing = await session.execute(
            select(DealJobs).where(
                col(DealJobs.organisation_id) == organisation_id,
                col(DealJobs.job_type) == DealJobType.ACCOUNT_RESEARCH,
                col(DealJobs.status).in_(ACTIVE_JOB_STATUSES),
            )
        )
        for job in existing.scalars().all():
            if (job.job_metadata or {}).get("company_id") == str(company_id):
                return str(job.job_id)

        job = DealJobs(
            organisation_id=organisation_id,
            user_id=user_id or AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.ACCOUNT_RESEARCH,
            status=JobStatus.PENDING,
            job_metadata={
                "company_id": str(company_id),
                "domain": domain,
                "linkedin_url": linkedin_url,
                "trigger": trigger,
                "force_refresh": force_refresh,
                "source": "api" if user_id else "automated_pipeline",
            },
        )
        session.add(job)
        await ActivityFeedProjectionService(
            session,
            organisation_id,
        ).upsert_account_enrichment_triggered(
            account,
            job_id=job.job_id,
            actor_user_id=user_id,
        )
        await session.commit()
        job_id = str(job.job_id)

    await send_deal_task_async(
        "account_research.process_job",
        kwargs={"organisation_id": str(organisation_id), "job_id": job_id},
        queue="deal_jobs",
    )
    return job_id


def _safe_domain(value: str | None) -> str | None:
    domain = AccountDomainResolutionService.normalize_domain(value)
    if not AccountDomainResolutionService.domain_is_safe_for_account_resolution(domain):
        return None
    return domain


def _is_recent(last_researched_at: datetime | None) -> bool:
    if not last_researched_at:
        return False
    return last_researched_at >= datetime.now(UTC) - timedelta(days=ACCOUNT_RESEARCH_COOLDOWN_DAYS)
