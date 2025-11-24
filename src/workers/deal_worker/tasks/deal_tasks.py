from __future__ import annotations

from contextlib import suppress
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
    """
    logger.info(f"Starting deal meeting processing for org {organisation_id}")

    async def _run():
        # Convert string UUID to UUID object
        org_uuid = UUID(organisation_id)

        # Acquire session
        session_gen = acquire_org_async_session(org_uuid)
        session = await anext(session_gen)
        try:
            return await process_deal_meetings(session, org_uuid)
        finally:
            with suppress(StopAsyncIteration):
                await anext(session_gen)

    try:
        result = run_async(_run())
        logger.info(f"Deal meeting processing completed for org {organisation_id}: {result}")
        return result
    except Exception as e:
        logger.error(
            f"Deal meeting processing failed for org {organisation_id}: {e}", exc_info=True
        )
        raise self.retry(exc=e, countdown=60, max_retries=3) from e


@app.task(name="deal.trigger_deal_meeting_processing_all_orgs")
def trigger_deal_meeting_processing_all_orgs():
    """
    Periodic task to trigger deal meeting processing for all active organisations.
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
                process_deal_meetings_task.delay(str(org.organisation_id))

    run_async(_run())


@app.task(bind=True, name="deal.process_single_deal_task", acks_late=True, max_retries=3)
def process_single_deal_task(self: Task, organisation_id: str, deal_id: str) -> dict[str, int]:
    """
    Celery task to process meetings for a single deal.
    """
    logger.info(f"Starting deal meeting processing for deal {deal_id} in org {organisation_id}")

    async def _run():
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)

        session_gen = acquire_org_async_session(org_uuid)
        session = await anext(session_gen)
        try:
            matcher = DealMeetingMatcher(session, org_uuid)
            return await matcher.process_deal(deal_uuid)
        finally:
            with suppress(StopAsyncIteration):
                await anext(session_gen)

    try:
        result = run_async(_run())
        logger.info(f"Deal meeting processing completed for deal {deal_id}: {result}")
        return result
    except Exception as e:
        logger.error(f"Deal meeting processing failed for deal {deal_id}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60, max_retries=3) from e
