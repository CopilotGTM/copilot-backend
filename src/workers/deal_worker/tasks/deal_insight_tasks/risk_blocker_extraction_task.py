"""Celery tasks for AI-powered risk/blocker extraction.

This module provides Celery tasks that extract risks and blockers from
deal timeline content (meetings, emails, Slack messages) using AI:
    - Identifies potential risks and blockers
    - Classifies by category, severity, and type
    - Deduplicates against existing items
    - Creates AI_SUGGESTED entries for user review

Tasks:
    - extract_deal_risks_blockers_task: Extract for a specific deal
    - trigger_extraction_all_orgs: Periodic task for all organisations

The tasks delegate to RiskBlockerExtractionService which uses
the RiskBlockerExtractionAgent for AI analysis.

See docs/Technical Documentation/deal/risk-blocker-extraction-tasks.md for full documentation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.infra.database.factory import get_async_provider
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services.risk_blocker_extraction_service import (
    RiskBlockerExtractionService,
)

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="risk_blocker.extract_deal_risks_blockers_task",
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,  # 10 minutes soft limit (AI extraction per deal)
    time_limit=660,  # 11 minutes hard limit
)
def extract_deal_risks_blockers_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Extract risks and blockers for a specific deal.

    This task is triggered by backend events (e.g., after new meetings/emails sync),
    not by frontend requests. It runs AI extraction and creates AI_SUGGESTED items.

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        deal_id: UUID of the deal (string for JSON serialization)
        force: Whether to re-process all sources

    Returns:
        Dictionary with extraction results
    """
    logger.info(
        "Starting risk/blocker extraction for deal %s in org %s",
        deal_id,
        organisation_id,
    )

    async def _run() -> dict[str, Any]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)

        # Service manages its own sessions (fresh session pattern)
        service = RiskBlockerExtractionService(org_uuid)
        return await service.process_deal(deal_uuid, force=force)

    try:
        result = run_async(_run())
        logger.info("Risk/blocker extraction completed for deal %s: %s", deal_id, result)
        return result
    except Exception as exc:
        logger.error(
            "Risk/blocker extraction failed for deal %s in org %s: %s",
            deal_id,
            organisation_id,
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc


@app.task(
    bind=True,
    name="risk_blocker.trigger_extraction_all_orgs",
    acks_late=True,
    soft_time_limit=1800,  # 30 minutes soft limit
    time_limit=1860,  # 31 minutes hard limit
)
def trigger_extraction_all_orgs(self: Task) -> dict[str, int]:
    """Trigger risk/blocker extraction for all active organisations.

    This is a periodic task that queues extraction jobs for all
    active organisations with deals.

    Returns:
        Dictionary with count of triggered jobs
    """
    logger.info("Starting risk/blocker extraction trigger for all orgs")

    async def _run() -> dict[str, int]:
        provider = get_async_provider()
        async with provider.master_session() as master_session:
            # Get all active organisations
            result = await master_session.execute(
                select(Organisation).where(col(Organisation.status) == OrganisationStatus.APPROVED)
            )
            orgs = result.scalars().all()

        triggered_count = 0
        for org in orgs:
            try:
                # Queue extraction for each org's deals
                async for session in acquire_org_async_session(org.organisation_id):
                    # Get only OPEN deals (skip won/lost/closed deals)
                    deal_result = await session.execute(
                        select(col(CRMDeal.deal_id))
                        .where(
                            col(CRMDeal.organisation_id) == org.organisation_id,
                            col(CRMDeal.is_deleted).is_(False),
                            col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                        )
                        .limit(100)
                    )
                    deal_ids = [str(row[0]) for row in deal_result.all()]

                    for deal_id in deal_ids:
                        extract_deal_risks_blockers_task.delay(
                            organisation_id=str(org.organisation_id),
                            deal_id=deal_id,
                        )
                        triggered_count += 1

            except Exception as e:
                logger.warning(
                    "Failed to trigger extraction for org %s: %s",
                    org.organisation_id,
                    e,
                )

        return {"triggered_jobs": triggered_count}

    try:
        result = run_async(_run())
        logger.info("Extraction trigger completed: %s", result)
        return result
    except Exception as exc:
        logger.error("Extraction trigger failed: %s", exc, exc_info=True)
        raise
