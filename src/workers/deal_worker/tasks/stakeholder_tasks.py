from __future__ import annotations

from contextlib import suppress
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlmodel import col

from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.stakeholder_service import StakeholderService

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="stakeholder.sync_from_meeting_task",
    acks_late=True,
    max_retries=3,
)
def sync_stakeholders_from_meeting_task(
    self: Task, organisation_id: str, deal_id: str, event_id: str
) -> dict[str, int]:
    """Sync stakeholders for a single deal/event association triggered via API."""
    logger.info(
        "Syncing stakeholders for org %s deal %s event %s",
        organisation_id,
        deal_id,
        event_id,
    )

    async def _run() -> dict[str, int]:
        org_uuid = UUID(organisation_id)
        deal_uuid = UUID(deal_id)
        event_uuid = UUID(event_id)

        session_gen = acquire_org_async_session(org_uuid)
        session = await anext(session_gen)
        try:
            deal_stmt = select(CRMDeal).where(
                col(CRMDeal.organisation_id) == org_uuid,
                col(CRMDeal.deal_id) == deal_uuid,
            )
            deal_result = await session.execute(deal_stmt)
            deal = deal_result.scalars().first()
            if not deal:
                logger.warning("Deal %s not found in org %s", deal_id, organisation_id)
                return {"stakeholders_synced": 0}

            event_stmt = (
                select(CalendarEvent)
                .options(selectinload(CalendarEvent.attendees))
                .where(
                    col(CalendarEvent.organisation_id) == org_uuid,
                    col(CalendarEvent.event_id) == event_uuid,
                )
            )
            event_result = await session.execute(event_stmt)
            event = event_result.scalars().first()
            if not event:
                logger.warning("Event %s not found in org %s", event_id, organisation_id)
                return {"stakeholders_synced": 0}

            service = StakeholderService(session, org_uuid)
            synced = await service.sync_stakeholders_from_event(deal, event)
            logger.info(
                "Stakeholder sync completed for deal %s event %s: %s",
                deal_id,
                event_id,
                synced,
            )
            return {"stakeholders_synced": synced}
        finally:
            with suppress(StopAsyncIteration):
                await anext(session_gen)

    try:
        return run_async(_run())
    except Exception as exc:  # pragma: no cover - Celery handles retries
        logger.error(
            "Stakeholder sync failed for org %s deal %s event %s: %s",
            organisation_id,
            deal_id,
            event_id,
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=60, max_retries=3) from exc
