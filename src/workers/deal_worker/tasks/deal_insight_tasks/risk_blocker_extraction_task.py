"""Celery tasks for AI-powered risk/blocker extraction.

This module provides Celery tasks that extract risks and blockers from
deal activity content (meetings, emails, Slack messages) using AI:
    - Identifies potential risks and blockers
    - Classifies by category, severity, and type
    - Deduplicates against existing items
    - Creates OPEN entries directly (no separate review/confirm step)

Tasks:
    - extract_deal_risks_blockers_task: Extract for a specific deal
    - trigger_extraction_all_orgs: Periodic task for all organisations

The tasks delegate to RiskBlockerExtractionService which uses
the RiskBlockerExtractionAgent for AI analysis.

See docs/Technical Documentation/deal/risk-blocker-extraction-tasks.md for full documentation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.infra.database.factory import get_async_provider
from src.infra.database.sessions import acquire_org_async_session
from src.infra.observability.context import correlation_headers
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_insight_services.risk_blocker_extraction_service import (
    RiskBlockerExtractionService,
)
from src.workers.deal_worker.utils.pipeline_dispatch import AUTOMATED_PIPELINE_USER_ID

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
    job_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Extract risks and blockers for a specific deal.

    This task is triggered by backend events (e.g., after new meetings/emails sync),
    not by frontend requests. It runs AI extraction and creates OPEN items directly.

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        deal_id: UUID of the deal (string for JSON serialization)
        job_id: Optional pre-created DealJobs ID for costing. Created inline if absent.
        force: Whether to re-process all sources

    Returns:
        Dictionary with extraction results
    """
    logger.info(
        "Starting risk/blocker extraction",
        extra={
            "event": "risk_blocker_extraction_started",
            "organisation_id": organisation_id,
            "deal_id": deal_id,
            "job_id": job_id,
            "force": force,
        },
    )

    async def _run() -> dict[str, Any]:
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
        service = RiskBlockerExtractionService(
            org_uuid,
            inference_run_id=job_uuid,
        )
        try:
            result = await service.process_deal(deal_uuid, force=force)
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
            "Risk/blocker extraction completed",
            extra={
                "event": "risk_blocker_extraction_completed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "job_id": job_id,
            },
        )
        return result
    except Exception as exc:
        logger.error(
            "Risk/blocker extraction failed",
            extra={
                "event": "risk_blocker_extraction_failed",
                "organisation_id": organisation_id,
                "deal_id": deal_id,
                "job_id": job_id,
                "failure_reason": str(exc),
            },
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
    logger.info(
        "Starting risk/blocker extraction trigger for all organisations",
        extra={"event": "risk_blocker_extraction_trigger_started"},
    )

    async def _run() -> dict[str, int]:
        provider = get_async_provider()
        async with provider.master_session() as master_session:
            # Read the approved organisation catalogue with the master session;
            # each organisation is handled later through its own tenant session.
            result = await master_session.execute(
                select(Organisation).where(col(Organisation.status) == OrganisationStatus.APPROVED)
            )
            orgs = result.scalars().all()

        triggered_count = 0
        for org in orgs:
            try:
                # Queue extraction for each org's deals
                async for session in acquire_org_async_session(org.organisation_id):
                    # Analyze only open deals because closed outcomes no longer
                    # need active risk/blocker extraction.
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

                    for d_id in deal_ids:
                        # Persist the job before dispatching so a retry can find
                        # the durable work item instead of creating another one.
                        risk_job = DealJobs(
                            organisation_id=org.organisation_id,
                            user_id=AUTOMATED_PIPELINE_USER_ID,
                            job_type=DealJobType.RISK_BLOCKER_EXTRACTION,
                            deal_id=UUID(d_id),
                            status=JobStatus.PENDING,
                            job_metadata={"source": "periodic_trigger"},
                        )
                        session.add(risk_job)
                        await session.flush()

                        # Preserve the periodic trigger lineage so child extraction tasks stay attributable.
                        extract_deal_risks_blockers_task.apply_async(
                            kwargs={
                                "organisation_id": str(org.organisation_id),
                                "deal_id": d_id,
                                "job_id": str(risk_job.job_id),
                            },
                            headers=correlation_headers(),
                        )
                        triggered_count += 1

                    await session.commit()

            except Exception as e:
                logger.warning(
                    "Failed to trigger extraction for organisation",
                    extra={
                        "event": "risk_blocker_extraction_org_trigger_failed",
                        "organisation_id": str(org.organisation_id),
                        "failure_reason": str(e),
                    },
                )

        return {"triggered_jobs": triggered_count}

    try:
        result = run_async(_run())
        logger.info(
            "Extraction trigger completed",
            extra={
                "event": "risk_blocker_extraction_trigger_completed",
                "triggered_jobs": result.get("triggered_jobs"),
            },
        )
        return result
    except Exception as exc:
        logger.error(
            "Extraction trigger failed",
            extra={
                "event": "risk_blocker_extraction_trigger_failed",
                "failure_reason": str(exc),
            },
            exc_info=True,
        )
        raise
