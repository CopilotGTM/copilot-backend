"""Celery tasks for stakeholder research via LLM agent.

Tasks:
- process_stakeholder_research_job: Process a single research job (Tier 3)
- retry_pending_research_org: Find un-researched stakeholders and dispatch (Tier 2)
- trigger_retry_pending_research_all_orgs: Dispatch to all orgs (Tier 1)
"""

from __future__ import annotations

from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import (
    Organisation,
    OrganisationStatus,
)
from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
)
from src.infra.database.factory import get_async_provider
from src.infra.database.sessions import acquire_org_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.infra.utils.task_lock import acquire_task_lock, release_task_lock
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services import (
    StakeholderResearchError,
    StakeholderResearchService,
)

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="stakeholder_research.process_job",
    acks_late=True,
    max_retries=2,
    soft_time_limit=120,
    time_limit=150,
)
def process_stakeholder_research_job(self: Task, organisation_id: str, job_id: str):
    """Research a single stakeholder via LLM agent.

    Args:
        organisation_id: The organisation ID (string for JSON serialization)
        job_id: The DealJobs ID to process
    """
    logger.info(
        "Stakeholder research job started",
        extra={
            "event": "stakeholder_research_started",
            "organisation_id": organisation_id,
            "job_id": job_id,
        },
    )

    lock_key = f"task_lock:stakeholder_research:{job_id}"
    if not acquire_task_lock(lock_key, ttl=180):
        logger.info(
            "Stakeholder research job skipped",
            extra={
                "event": "stakeholder_research_skipped",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "skip_reason": "already_running",
            },
        )
        return {"status": "skipped", "reason": "already_running"}

    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        # Phase 1: Load job context (fresh session)
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Stakeholder research job {job_id} not found")

            stakeholder_id: UUID | None = job.stakeholder_id
            deal_id: UUID | None = job.deal_id

            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        if not stakeholder_id:
            raise RuntimeError(f"Stakeholder research job {job_id} has no stakeholder_id")

        # Phase 2: Call service
        service = StakeholderResearchService(org_uuid)
        try:
            markdown = await service.research_stakeholder(
                stakeholder_id=stakeholder_id,
                deal_id=deal_id,
            )
        except StakeholderResearchError as e:
            logger.error(
                "Stakeholder research failed",
                extra={
                    "event": "stakeholder_research_failed",
                    "organisation_id": organisation_id,
                    "job_id": job_id,
                    "stakeholder_id": str(stakeholder_id),
                    "deal_id": str(deal_id) if deal_id else None,
                    "failure_reason": e.message,
                },
            )
            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = e.message
                    await session.commit()
            if not e.retryable:
                return {"status": "failed", "error": e.message}
            raise

        # Phase 3: Save result + token usage (fresh session)
        token_usage = service.last_token_usage
        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = {"research_length": len(markdown)}
                if token_usage:
                    job.token_usage = token_usage.to_dict()
                    job.model_name = token_usage.model_name
                    if token_usage.cost is not None:
                        from decimal import Decimal

                        job.cost = Decimal(str(token_usage.cost))
                    if token_usage.duration_seconds is not None:
                        job.latency_ms = int(token_usage.duration_seconds * 1000)
                await session.commit()

        logger.info(
            "Stakeholder research completed",
            extra={
                "event": "stakeholder_research_completed",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "stakeholder_id": str(stakeholder_id),
                "deal_id": str(deal_id) if deal_id else None,
            },
        )
        return {"status": "completed", "job_id": job_id}

    try:
        return run_async(_run())
    except Exception as e:
        logger.error(
            "Stakeholder research task failed",
            extra={
                "event": "stakeholder_research_failed",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "failure_reason": str(e),
            },
            exc_info=True,
        )
        raise self.retry(exc=e, countdown=30 * (2**self.request.retries)) from e
    finally:
        release_task_lock(lock_key)


# Max stakeholders to dispatch per org per run to avoid overwhelming the queue
_RETRY_BATCH_LIMIT = 20


@app.task(bind=True, name="stakeholder_research.retry_pending_org", acks_late=True, max_retries=2)
def retry_pending_research_org(self: Task, organisation_id: str):
    """Tier 2: Find approved stakeholders without research and dispatch jobs.

    Picks up stakeholders where last_researched_at IS NULL — either never
    researched or a previous attempt hit an enrichment error (quality gate
    clears last_researched_at on tool-level failures so they're retried).
    """
    lock_key = f"task_lock:stakeholder_research_retry:{organisation_id}"
    if not acquire_task_lock(lock_key, ttl=1800):
        return {"status": "skipped", "reason": "already_running"}

    try:
        from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
            dispatch_stakeholder_research_async,
        )

        async def _run():
            org_uuid = UUID(organisation_id)

            async for session in acquire_org_async_session(org_uuid):
                stmt = (
                    select(DealStakeholder)
                    .where(
                        col(DealStakeholder.organisation_id) == org_uuid,
                        col(DealStakeholder.approval_status) == ApprovalStatus.APPROVED,
                        col(DealStakeholder.last_researched_at).is_(None),
                    )
                    .limit(_RETRY_BATCH_LIMIT)
                )
                result = await session.execute(stmt)
                pending = result.scalars().all()

                # Extract IDs before releasing session
                stakeholder_infos = [(s.stakeholder_id, s.deal_id) for s in pending]

            dispatched = 0
            for s_id, d_id in stakeholder_infos:
                try:
                    job_id = await dispatch_stakeholder_research_async(
                        organisation_id=org_uuid,
                        stakeholder_id=s_id,
                        deal_id=d_id,
                    )
                    if job_id:
                        dispatched += 1
                except Exception:
                    logger.warning(
                        "Failed to dispatch research for stakeholder",
                        extra={
                            "event": "stakeholder_research_retry_dispatch_failed",
                            "organisation_id": organisation_id,
                            "stakeholder_id": str(s_id),
                        },
                        exc_info=True,
                    )

            logger.info(
                "Stakeholder research retry completed for organisation",
                extra={
                    "event": "stakeholder_research_retry_completed",
                    "organisation_id": organisation_id,
                    "found_count": len(stakeholder_infos),
                    "dispatched_count": dispatched,
                },
            )
            return {
                "status": "completed",
                "found": len(stakeholder_infos),
                "dispatched": dispatched,
            }

        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Stakeholder research retry failed for organisation",
            extra={
                "event": "stakeholder_research_retry_failed",
                "organisation_id": organisation_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=120) from exc
    finally:
        release_task_lock(lock_key)


@app.task(
    bind=False,
    name="stakeholder_research.trigger_retry_all_orgs",
    acks_late=True,
    max_retries=0,
)
def trigger_retry_pending_research_all_orgs():
    """Tier 1: Dispatch pending research retry to all approved organisations."""

    async def _dispatch():
        provider = get_async_provider()
        async with provider.master_session() as master_session:
            result = await master_session.execute(
                select(Organisation).where(col(Organisation.status) == OrganisationStatus.APPROVED)
            )
        orgs = result.scalars().all()

        dispatched = 0
        for org in orgs:
            try:
                # Keep all-org coordinator context attached to each per-org retry sweep.
                retry_pending_research_org.apply_async(
                    kwargs={"organisation_id": str(org.organisation_id)},
                    headers=correlation_headers(),
                )
                dispatched += 1
            except Exception as e:
                logger.warning(
                    "Failed to dispatch retry for organisation",
                    extra={
                        "event": "stakeholder_research_retry_dispatch_org_failed",
                        "organisation_id": str(org.organisation_id),
                        "failure_reason": str(e),
                    },
                )

        return {"status": "completed", "orgs_dispatched": dispatched}

    return run_async(_dispatch())
