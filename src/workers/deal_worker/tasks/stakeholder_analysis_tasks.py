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

from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.infra.database.factory import get_async_provider
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.stakeholder_analysis_service import (
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
    self: Task, organisation_id: str, deal_id: str
) -> dict[str, int]:
    """Analyze all stakeholders for a specific deal using meeting transcripts.

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        deal_id: UUID of the deal (string for JSON serialization)

    Returns:
        Dictionary with processing statistics
    """
    logger.info("Starting stakeholder analysis for deal %s in org %s", deal_id, organisation_id)

    async def _run() -> dict[str, int]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)

        async for session in acquire_org_async_session(org_uuid):
            service = StakeholderAnalysisService(session, org_uuid)
            return await service.analyze_deal_by_id(deal_uuid)

    try:
        result = run_async(_run())
        logger.info("Stakeholder analysis completed for deal %s: %s", deal_id, result)
        return result
    except Exception as exc:
        logger.error(
            "Stakeholder analysis failed for deal %s in org %s: %s",
            deal_id,
            organisation_id,
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc


@app.task(
    bind=True,
    name="stakeholder.analyze_org_stakeholders_task",
    acks_late=True,
    max_retries=3,
)
def analyze_org_stakeholders_task(self: Task, organisation_id: str) -> dict[str, int]:
    """Analyze all stakeholders across all deals for an organisation.

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)

    Returns:
        Dictionary with processing statistics
    """
    logger.info("Starting stakeholder analysis for org %s", organisation_id)

    async def _run() -> dict[str, int]:
        org_uuid = UUID(organisation_id)

        async for session in acquire_org_async_session(org_uuid):
            service = StakeholderAnalysisService(session, org_uuid)
            return await service.analyze_all_stakeholders_for_organisation()

    try:
        result = run_async(_run())
        logger.info("Stakeholder analysis completed for org %s: %s", organisation_id, result)
        return result
    except Exception as exc:
        logger.error(
            "Stakeholder analysis failed for org %s: %s",
            organisation_id,
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc


@app.task(name="stakeholder.trigger_analysis_all_orgs")
def trigger_stakeholder_analysis_all_orgs() -> None:
    """Periodic task to trigger stakeholder analysis for all active organisations."""
    logger.info("Triggering stakeholder analysis for all organisations")

    async def _run() -> None:
        provider = get_async_provider()
        async with provider.master_session() as session:
            stmt = select(Organisation).where(Organisation.status == OrganisationStatus.APPROVED)
            result = await session.execute(stmt)
            orgs = result.scalars().all()

            logger.info("Found %d active organisations for stakeholder analysis", len(orgs))

            for org in orgs:
                analyze_org_stakeholders_task.apply_async(
                    args=[str(org.organisation_id)],
                    headers=correlation_headers(),
                )

    run_async(_run())
