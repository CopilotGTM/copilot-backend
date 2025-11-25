"""CRM service layer for managing deal meetings."""

from __future__ import annotations

import os
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.api.schemas.crm_models import (
    DealMeetingResponse,
    MeetingAttendee,
)
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app as deal_worker_app
from src.workers.deal_worker.services.briefing_generator import (
    BriefingData,
    BriefingGeneratorService,
)
from src.workers.deal_worker.services.stakeholder_service import StakeholderService

logger = get_logger("meetings_service")

STAKEHOLDER_TASK_NAME = "stakeholder.sync_from_meeting_task"
ASYNC_STAKEHOLDER_SYNC_DEFAULT = os.getenv("STAKEHOLDER_SYNC_VIA_TASK", "true").lower() not in (
    "0",
    "false",
    "no",
)


class MeetingsServiceError(Exception):
    """Custom exception for meetings service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MeetingsService:
    """Service for managing CRM deal meetings."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def list_meetings_by_deal(self, deal_id: UUID) -> list[DealMeetingResponse]:
        """Get all meetings associated with a specific deal."""
        stmt = (
            select(CalendarEventDealAssociation, CalendarEvent, CRMDeal)
            .join(
                CalendarEvent,
                col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
            )
            .join(CRMDeal, col(CalendarEventDealAssociation.deal_id) == col(CRMDeal.deal_id))
            .options(selectinload(cast(Any, CalendarEvent.attendees)))
            .where(
                col(CalendarEventDealAssociation.deal_id) == deal_id,
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            )
        )
        result = await self.session.execute(stmt)
        rows = result.all()

        meetings = []
        for assoc, event, deal in rows:
            attendees = [
                MeetingAttendee(
                    name=att.display_name,
                    email=att.email,
                    status=att.response_status,
                )
                for att in event.attendees
            ]

            meetings.append(
                DealMeetingResponse(
                    event_id=event.event_id,
                    subject=event.data_payload.get("subject") if event.data_payload else None,
                    start_at=event.start_at,
                    status=assoc.status,
                    stage=deal.stage_label or deal.stage_name,
                    attendees=attendees,
                    meeting_prep_status=assoc.meeting_prep_status,
                    meeting_prep_data=BriefingData(**assoc.meeting_prep_data)
                    if assoc.meeting_prep_data
                    else None,
                )
            )
        return meetings

    async def generate_briefing(self, deal_id: UUID, event_id: UUID) -> BriefingData | None:
        """Trigger the generation of a meeting briefing using AI."""
        service = BriefingGeneratorService(self.session, organisation_id=self.organisation_id)
        return await service.generate_briefing(deal_id, event_id)

    async def update_meeting_status(
        self, deal_id: UUID, event_id: UUID, status: AssociationStatus
    ) -> None:
        """Update the status of a meeting association."""
        stmt = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.event_id) == event_id,
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        association = result.scalars().first()

        if not association:
            raise MeetingsServiceError("Meeting association not found.", status_code=404)

        association.status = status
        await self.session.commit()

        if status == AssociationStatus.CONFIRMED:
            await self._trigger_stakeholder_sync(deal_id, event_id)

    async def _trigger_stakeholder_sync(
        self,
        deal_id: UUID,
        event_id: UUID,
        use_async: bool = ASYNC_STAKEHOLDER_SYNC_DEFAULT,
    ) -> None:
        """Trigger stakeholder sync via task when enabled, fallback to inline."""
        if use_async and self._enqueue_stakeholder_task(deal_id, event_id):
            return

        await self._sync_stakeholders_inline(deal_id, event_id)

    def _enqueue_stakeholder_task(self, deal_id: UUID, event_id: UUID) -> bool:
        """Enqueue stakeholder sync task; return True on success."""
        try:
            deal_worker_app.send_task(
                STAKEHOLDER_TASK_NAME,
                args=[str(self.organisation_id), str(deal_id), str(event_id)],
            )
            return True
        except Exception as exc:
            logger.warning(
                "Failed to enqueue stakeholder sync for org %s deal %s event %s: %s",
                self.organisation_id,
                deal_id,
                event_id,
                exc,
            )
            return False

    async def _sync_stakeholders_inline(self, deal_id: UUID, event_id: UUID) -> None:
        """Run stakeholder sync inline for the provided deal/event."""
        deal_participants_rel = cast(Any, CRMDeal.participants)
        deal_stmt = (
            select(CRMDeal)
            .options(selectinload(deal_participants_rel))
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_id) == deal_id,
            )
        )
        deal_result = await self.session.execute(deal_stmt)
        deal = deal_result.scalars().first()
        if not deal:
            logger.warning("Deal %s not found for inline stakeholder sync", deal_id)
            return

        event_stmt = (
            select(CalendarEvent)
            .options(selectinload(cast(Any, CalendarEvent.attendees)))
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.event_id) == event_id,
            )
        )
        event_result = await self.session.execute(event_stmt)
        event = event_result.scalars().first()
        if not event:
            logger.warning("Event %s not found for inline stakeholder sync", event_id)
            return

        service = StakeholderService(self.session, self.organisation_id)
        await service.sync_stakeholders_from_event(deal, event)
        await self.session.commit()

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("MeetingsService close exception", exc_info=True)
