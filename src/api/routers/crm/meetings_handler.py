from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.workers.deal_worker.services.briefing_generator import (
    BriefingData,
    BriefingGeneratorService,
)

meetings_router = APIRouter()


class DealMeetingResponse(BaseModel):
    event_id: UUID
    subject: str | None
    start_at: datetime
    status: AssociationStatus
    meeting_prep_status: GenerationStatus
    meeting_prep_data: BriefingData | None = None

    model_config = {"from_attributes": True}


@meetings_router.get(
    "/{deal_id}/meetings",
    response_model=GenericResponse[list[DealMeetingResponse]],
    summary="Get meetings for a deal",
)
async def get_deal_meetings(
    deal_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """
    Get all meetings associated with a specific deal.
    """
    stmt = (
        select(CalendarEventDealAssociation, CalendarEvent)
        .join(
            CalendarEvent, col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id)
        )
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == user_ids.organisation_id,
        )
    )
    result = await session.execute(stmt)
    rows = result.all()

    meetings = []
    for assoc, event in rows:
        meetings.append(
            DealMeetingResponse(
                event_id=event.event_id,
                subject=event.data_payload.get("subject") if event.data_payload else None,
                start_at=event.start_at,
                status=assoc.status,
                meeting_prep_status=assoc.meeting_prep_status,
                meeting_prep_data=BriefingData(**assoc.meeting_prep_data)
                if assoc.meeting_prep_data
                else None,
            )
        )

    return GenericResponse(
        status="SUCCESS",
        message="Meetings retrieved successfully",
        data=meetings,
    )


@meetings_router.post(
    "/{deal_id}/meetings/{event_id}/briefing",
    response_model=GenericResponse[BriefingData],
    summary="Generate a meeting briefing",
)
async def generate_meeting_briefing(
    deal_id: UUID,
    event_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """
    Trigger the generation of a meeting briefing using AI.
    """
    service = BriefingGeneratorService(session, organisation_id=user_ids.organisation_id)
    briefing = await service.generate_briefing(deal_id, event_id)

    if not briefing:
        raise HTTPException(
            status_code=404, detail="Could not generate briefing. Check deal/event existence."
        )

    return GenericResponse(
        status="SUCCESS",
        message="Briefing generated successfully",
        data=briefing,
    )


class UpdateMeetingStatusRequest(BaseModel):
    status: AssociationStatus


@meetings_router.patch(
    "/{deal_id}/meetings/{event_id}/status",
    response_model=GenericResponse[None],
    summary="Update meeting association status",
)
async def update_meeting_status(
    deal_id: UUID,
    event_id: UUID,
    request: UpdateMeetingStatusRequest,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """
    Update the status of a meeting association (e.g., CONFIRM or REJECT).
    """
    stmt = select(CalendarEventDealAssociation).where(
        col(CalendarEventDealAssociation.deal_id) == deal_id,
        col(CalendarEventDealAssociation.event_id) == event_id,
        col(CalendarEventDealAssociation.organisation_id) == user_ids.organisation_id,
    )
    result = await session.execute(stmt)
    association = result.scalars().first()

    if not association:
        raise HTTPException(status_code=404, detail="Meeting association not found.")

    association.status = request.status
    await session.commit()

    return GenericResponse(
        status="SUCCESS",
        message=f"Meeting status updated to {request.status.value}",
    )
