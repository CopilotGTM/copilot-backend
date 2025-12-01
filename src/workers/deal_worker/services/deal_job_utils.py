"""Utility functions for deal job processing.

This module provides helper functions for loading deal-related entities
and building context for job handlers.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder


async def get_deal(session: AsyncSession, org_id: UUID, deal_id: UUID) -> CRMDeal | None:
    """Load a deal by ID.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID to load

    Returns:
        CRMDeal if found, None otherwise
    """
    stmt = select(CRMDeal).where(
        col(CRMDeal.deal_id) == deal_id,
        col(CRMDeal.organisation_id) == org_id,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_event(
    session: AsyncSession, org_id: UUID, event_id: UUID | None
) -> CalendarEvent | None:
    """Load a calendar event by ID.

    Args:
        session: Database session
        org_id: Organisation ID
        event_id: Event ID to load (can be None)

    Returns:
        CalendarEvent if found, None otherwise
    """
    if not event_id:
        return None
    stmt = select(CalendarEvent).where(
        col(CalendarEvent.event_id) == event_id,
        col(CalendarEvent.organisation_id) == org_id,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_stakeholders(
    session: AsyncSession, org_id: UUID, deal_id: UUID
) -> list[DealStakeholder]:
    """Load all stakeholders for a deal.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID to load stakeholders for

    Returns:
        List of DealStakeholder entities
    """
    stmt = select(DealStakeholder).where(
        col(DealStakeholder.deal_id) == deal_id,
        col(DealStakeholder.organisation_id) == org_id,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_association(
    session: AsyncSession, org_id: UUID, deal_id: UUID, event_id: UUID | None
) -> CalendarEventDealAssociation | None:
    """Load the association between a deal and a calendar event.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID
        event_id: Event ID (can be None)

    Returns:
        CalendarEventDealAssociation if found, None otherwise
    """
    if not event_id:
        return None
    stmt = select(CalendarEventDealAssociation).where(
        col(CalendarEventDealAssociation.deal_id) == deal_id,
        col(CalendarEventDealAssociation.event_id) == event_id,
        col(CalendarEventDealAssociation.organisation_id) == org_id,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


def build_briefing_context_string(
    deal: CRMDeal, event: CalendarEvent, stakeholders: list[DealStakeholder]
) -> str:
    """Build the context string for the briefing agent.

    Args:
        deal: The CRM deal
        event: The calendar event
        stakeholders: List of stakeholders for the deal

    Returns:
        Formatted context string for the briefing agent
    """
    stakeholder_info = "\n".join(
        [f"- {s.name} ({s.job_title}): {s.stance} (Temp: {s.temperature})" for s in stakeholders]
    )

    return f"""
    Meeting Context:
    - Event: {event.data_payload.get("subject", "Unknown") if event.data_payload else "Unknown"}
    - Date: {event.start_at}
    - Description: {event.data_payload.get("description", "") if event.data_payload else ""}

    Deal Context:
    - Name: {deal.name}
    - Stage: {deal.stage_id}
    - Amount: {deal.amount}
    - Close Date: {deal.close_date}

    Stakeholders:
    {stakeholder_info}
    """


def build_summary_context_string(
    deal: CRMDeal,
    event: CalendarEvent,
    transcript: str,
    stakeholders: list[DealStakeholder] | None = None,
) -> str:
    """Build the context string for the summary agent.

    Args:
        deal: The CRM deal
        event: The calendar event
        transcript: The meeting transcript text
        stakeholders: Optional list of stakeholders for the deal

    Returns:
        Formatted context string for the summary agent
    """
    stakeholder_info = ""
    if stakeholders:
        stakeholder_info = "\n".join(
            [f"- {s.name} ({s.job_title}): {s.stance}" for s in stakeholders]
        )

    attendees_info = ""
    if event.data_payload and event.data_payload.get("attendees"):
        attendees = event.data_payload.get("attendees", [])
        attendees_info = "\n".join(
            [f"- {a.get('name', 'Unknown')} ({a.get('email', '')})" for a in attendees]
        )

    return f"""
MEETING INFORMATION:
- Subject: {event.data_payload.get("subject", "Unknown") if event.data_payload else "Unknown"}
- Date: {event.start_at}
- Duration: {event.data_payload.get("duration", "Unknown") if event.data_payload else "Unknown"}

ATTENDEES:
{attendees_info or "No attendee information available"}

DEAL CONTEXT:
- Deal Name: {deal.name}
- Stage: {deal.stage_id}
- Amount: {deal.amount}
- Close Date: {deal.close_date}
- Company: {deal.company_name or "Unknown"}

KNOWN STAKEHOLDERS:
{stakeholder_info or "No stakeholder information available"}

MEETING TRANSCRIPT:
{transcript}
"""
