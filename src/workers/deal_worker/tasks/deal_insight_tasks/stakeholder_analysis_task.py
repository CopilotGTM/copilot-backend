"""Celery tasks for AI-powered stakeholder analysis.

This module provides Celery tasks that analyze stakeholders using meeting
transcripts to extract insights via AI:
    - Stance: Champion, Supporter, Neutral, Blocker, Unknown
    - Decision Power: Final Decision, Influencer, Recommender, End User
    - Temperature: Hot, Warm, Cold
    - Champion Potential: Likelihood score (0.0 - 1.0)
    - Motivations & Pain Points: Text insights from conversations

Tasks:
    - analyze_deal_stakeholders_task: Analyze all stakeholders for a specific deal
    - analyze_org_stakeholders_task: Analyze all stakeholders across an organisation
    - trigger_stakeholder_analysis_all_orgs: Periodic task for all organisations

The tasks delegate to StakeholderAnalysisService which batches all stakeholders
for a deal into a single LLM call for efficiency.

For stakeholder discovery/synchronization tasks, see stakeholder_tasks.py.
See docs/Technical Documentation/deal/deal-worker-tasks.md for full documentation.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services.stakeholder_analysis_service import (
    StakeholderAnalysisService,
)

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="stakeholder.analyze_deal_stakeholders_task",
    acks_late=True,
    max_retries=3,
)
def analyze_deal_stakeholders_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    job_id: str | None = None,
) -> dict[str, int]:
    """Analyze all stakeholders for a specific deal using meeting transcripts.

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        deal_id: UUID of the deal (string for JSON serialization)
        job_id: Optional pre-created DealJobs ID for costing. Created inline if absent.

    Returns:
        Dictionary with processing statistics
    """
    logger.info(
        "Starting stakeholder analysis",
        extra={
            "event": "stakeholder_analysis_started",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "job_id": job_id,
        },
    )

    async def _run() -> dict[str, int]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)
        job_uuid: UUID | None = UUID(job_id) if job_id else None

        # Phase 1: Mark job as IN_PROGRESS (if job exists)
        if job_uuid:
            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.IN_PROGRESS
                    await session.commit()

        # Phase 2: Call service (service manages its own sessions)
        service = StakeholderAnalysisService(org_uuid)
        try:
            result = await service.analyze_deal_by_id(deal_uuid)
        except Exception as e:
            if job_uuid:
                async for session in acquire_org_async_session(org_uuid):
                    job = await session.get(DealJobs, job_uuid)
                    if job:
                        job.status = JobStatus.FAILED
                        job.error_message = str(e)[:500]
                        await session.commit()
            raise

        # Phase 3: Save costing (fresh session)
        if job_uuid:
            token_usage = service.last_token_usage
            async for session in acquire_org_async_session(org_uuid):
                job = await session.get(DealJobs, job_uuid)
                if job:
                    job.status = JobStatus.COMPLETED
                    job.result_data = result
                    if token_usage:
                        job.token_usage = token_usage.to_dict()
                        job.model_name = token_usage.model_name
                        if token_usage.cost is not None:
                            job.cost = Decimal(str(token_usage.cost))
                        if token_usage.duration_seconds is not None:
                            job.latency_ms = int(token_usage.duration_seconds * 1000)
                    await session.commit()

        return result

    try:
        result = run_async(_run())
        logger.info(
            "Stakeholder analysis completed",
            extra={
                "event": "stakeholder_analysis_completed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "job_id": job_id,
            },
        )
        return result
    except Exception as exc:
        logger.error(
            "Stakeholder analysis failed",
            extra={
                "event": "stakeholder_analysis_failed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "job_id": job_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc


# NOTE: analyze_org_stakeholders_task and trigger_stakeholder_analysis_all_orgs
# are disabled. Stakeholder analysis is now triggered exclusively via the deal
# insight pipeline (dispatch_deal_insight_pipeline) which creates a DealJobs
# record before dispatch for proper costing.
