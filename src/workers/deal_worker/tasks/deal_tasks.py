"""Celery tasks for deal-meeting matching and stakeholder discovery.

This module provides Celery tasks that orchestrate the deal-meeting matching process:
- Match calendar events to CRM deals based on team and contact participation
- Discover stakeholders from meeting attendees
- Handle cancelled meeting associations

Tasks:
    - process_deal_meetings_task: Process all deals for a single organisation
    - process_single_deal_task: Process meetings for a specific deal
    - trigger_deal_meeting_processing_all_orgs: Periodic task for all organisations

The tasks delegate to DealMeetingMatcher service for the actual matching logic.
See docs/Technical Documentation/deal/deal-meeting-automation.md for algorithm details.
"""

from __future__ import annotations

from uuid import UUID

from celery import Task
from sqlalchemy import select

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.master_tables.organisation_models import Organisation, OrganisationStatus
from src.infra.database.factory import get_async_provider
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_meeting_matcher import (
    DealMeetingMatcher,
    process_deal_meetings,
)

logger = get_logger(__name__)


@app.task(bind=True, name="deal.process_deal_meetings_task", acks_late=True, max_retries=3)
def process_deal_meetings_task(self: Task, organisation_id: str) -> dict[str, int]:
    """
    Celery task to process deal meetings for an organisation.

    Matches calendar events to deals and discovers stakeholders from:
    1. CRM contact participants (directly added)
    2. Meeting attendees sharing the same domain as CRM contacts

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)

    Returns:
        Dictionary with processing results
    """
    logger.info(f"Starting deal meeting processing for org {organisation_id}")

    async def _run():
        org_uuid = UUID(organisation_id)
        async for session in acquire_org_async_session(org_uuid):
            return await process_deal_meetings(session, org_uuid)

    try:
        result = run_async(_run())
        logger.info(f"Deal meeting processing completed for org {organisation_id}: {result}")
        return result
    except Exception as e:
        logger.error(
            f"Deal meeting processing failed for org {organisation_id}: {e}", exc_info=True
        )
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e


@app.task(name="deal.trigger_deal_meeting_processing_all_orgs")
def trigger_deal_meeting_processing_all_orgs():
    """
    Periodic task to trigger deal meeting processing for all active organisations.

    This task runs on a schedule (every 3 hours) and enqueues individual
    processing tasks for each active organisation.
    """
    logger.info("Triggering deal meeting processing for all organisations")

    async def _run():
        provider = get_async_provider()
        async with provider.master_session() as session:
            # Fetch all active organisations
            stmt = select(Organisation).where(Organisation.status == OrganisationStatus.APPROVED)
            result = await session.execute(stmt)
            orgs = result.scalars().all()

            logger.info(f"Found {len(orgs)} active organisations to process")

            for org in orgs:
                # Enqueue task for each organisation
                process_deal_meetings_task.delay(str(org.organisation_id))

    run_async(_run())


@app.task(bind=True, name="deal.process_single_deal_task", acks_late=True, max_retries=3)
def process_single_deal_task(self: Task, organisation_id: str, deal_id: str) -> dict[str, int]:
    """
    Celery task to process meetings for a single deal.

    Matches calendar events to the deal and discovers stakeholders from:
    1. CRM contact participants (directly added)
    2. Meeting attendees sharing the same domain as CRM contacts

    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        deal_id: UUID of the deal to process (string for JSON serialization)

    Returns:
        Dictionary with processing results
    """
    logger.info(f"Starting deal meeting processing for deal {deal_id} in org {organisation_id}")

    async def _run():
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)
        async for session in acquire_org_async_session(org_uuid):
            matcher = DealMeetingMatcher(session, org_uuid)
            return await matcher.process_deal(deal_uuid)

    try:
        result = run_async(_run())
        logger.info(f"Deal meeting processing completed for deal {deal_id}: {result}")
        return result
    except Exception as e:
        logger.error(f"Deal meeting processing failed for deal {deal_id}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
