"""Utility functions for deal job processing.

This module provides helper functions for loading deal-related entities
and building context for job handlers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
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
    """Load a calendar event by ID with attendees eagerly loaded.

    Args:
        session: Database session
        org_id: Organisation ID
        event_id: Event ID to load (can be None)

    Returns:
        CalendarEvent if found (with attendees loaded), None otherwise
    """
    if not event_id:
        return None
    stmt = (
        select(CalendarEvent)
        .options(selectinload(cast(Any, CalendarEvent.attendees)))
        .where(
            col(CalendarEvent.event_id) == event_id,
            col(CalendarEvent.organisation_id) == org_id,
        )
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


async def get_previous_meetings(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
    current_event_start: datetime,
    limit: int = 5,
) -> list[CalendarEvent]:
    """Get previous meetings for a deal.

    Fetches meetings older than the current meeting that are associated with
    the deal. Used to provide historical context for briefing generation.
    Transcripts for these meetings should be fetched separately using TranscriptService.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID to find meetings for
        current_event_start: Start time of current meeting (filter meetings older than this)
        limit: Maximum number of previous meetings to return (default 5)

    Returns:
        List of CalendarEvent objects, ordered by start_at descending (most recent first)
    """
    stmt = (
        select(CalendarEvent)
        .join(
            CalendarEventDealAssociation,
            col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
        )
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == org_id,
            col(CalendarEvent.start_at) < current_event_start,
        )
        .order_by(col(CalendarEvent.start_at).desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_all_deal_meetings(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
    limit: int = 20,
) -> list[CalendarEvent]:
    """Get all meetings associated with a deal.

    Fetches all meetings associated with the deal, ordered chronologically
    (oldest first). Used for deal-level summary generation.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID to find meetings for
        limit: Maximum number of meetings to return (default 20)

    Returns:
        List of CalendarEvent objects, ordered by start_at ascending (oldest first)
    """
    stmt = (
        select(CalendarEvent)
        .options(selectinload(cast(Any, CalendarEvent.attendees)))
        .join(
            CalendarEventDealAssociation,
            col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
        )
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == org_id,
        )
        .order_by(col(CalendarEvent.start_at).asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _format_meeting_date(date_value: str) -> str:
    """Format a meeting date string for display.

    Args:
        date_value: Date string, possibly in ISO format

    Returns:
        Formatted date string
    """
    if "T" in date_value:
        try:
            dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return date_value


def _build_previous_transcripts_section(previous_meeting_transcripts: list[dict]) -> str:
    """Build the previous meetings transcripts section for the briefing context.

    Args:
        previous_meeting_transcripts: List of meeting transcript dicts with
            'subject', 'date', and 'transcript' keys

    Returns:
        Formatted string section for previous meeting transcripts
    """
    meetings_text = []
    for i, meeting in enumerate(previous_meeting_transcripts, 1):
        meeting_date = meeting.get("date", "Unknown date")
        if isinstance(meeting_date, str):
            meeting_date = _format_meeting_date(meeting_date)
        subject = meeting.get("subject", "Unknown Subject")
        transcript = meeting.get("transcript", "No transcript available")
        # Truncate very long transcripts to avoid context overflow
        if len(transcript) > 10000:
            transcript = transcript[:10000] + "\n... [transcript truncated for brevity]"
        meetings_text.append(f"### Meeting {i}: {subject} ({meeting_date})\n{transcript}\n")

    return f"""

PREVIOUS MEETING TRANSCRIPTS:
(Historical context from past meetings with this deal - most recent first)

{chr(10).join(meetings_text)}"""


def _get_event_title(event: CalendarEvent) -> str:
    """Get the title for a calendar event.

    Args:
        event: The calendar event

    Returns:
        Event title or 'Unknown'
    """
    if event.title:
        return event.title
    if event.data_payload:
        return cast(str, event.data_payload.get("subject")) or "Unknown"
    return "Unknown"


def _get_event_description(event: CalendarEvent) -> str:
    """Get the description for a calendar event.

    Args:
        event: The calendar event

    Returns:
        Event description or empty string
    """
    if event.description:
        return event.description
    if event.data_payload:
        return cast(str, event.data_payload.get("description")) or ""
    return ""


def _get_event_attendees_info(event: CalendarEvent) -> str:
    """Get formatted attendee information for a calendar event.

    Args:
        event: The calendar event

    Returns:
        Formatted string of attendees or empty string
    """
    # First try the relationship (if loaded)
    if event.attendees:
        return "\n".join([f"- {a.display_name or 'Unknown'} ({a.email})" for a in event.attendees])
    # Fallback to data_payload
    if event.data_payload and event.data_payload.get("attendees"):
        attendees = event.data_payload.get("attendees", [])
        return "\n".join(
            [f"- {a.get('name', 'Unknown')} ({a.get('email', '')})" for a in attendees]
        )
    return ""


def build_briefing_context_string(
    deal: CRMDeal,
    event: CalendarEvent,
    stakeholders: list[DealStakeholder],
    previous_meeting_transcripts: list[dict] | None = None,
) -> str:
    """Build the context string for the briefing agent.

    Args:
        deal: The CRM deal
        event: The calendar event (upcoming meeting)
        stakeholders: List of stakeholders for the deal
        previous_meeting_transcripts: Optional list of previous meeting transcripts.
            Each dict should contain 'subject', 'date', and 'transcript' keys.

    Returns:
        Formatted context string for the briefing agent
    """
    stakeholder_info = "\n".join(
        [f"- {s.name} ({s.job_title}): {s.stance} (Temp: {s.temperature})" for s in stakeholders]
    )

    # Get attendees for the upcoming meeting
    attendees_info = _get_event_attendees_info(event)

    # Build previous meetings section if transcripts are available
    previous_meetings_section = ""
    if previous_meeting_transcripts:
        previous_meetings_section = _build_previous_transcripts_section(
            previous_meeting_transcripts
        )

    # Format the meeting date nicely
    meeting_date = (
        event.start_at.strftime("%A, %B %d, %Y at %I:%M %p") if event.start_at else "Unknown"
    )

    return f"""
==========================================================
MEETING BRIEFING REQUEST
==========================================================

TARGET MEETING (Prepare briefing for THIS meeting):
- Subject: {_get_event_title(event)}
- Date/Time: {meeting_date}
- Organizer: {event.organiser_name or event.organiser_email or "Unknown"}
- Location: {event.location or "Online Meeting"}
- Conference Link: {event.conference_link or "N/A"}

MEETING ATTENDEES:
{attendees_info or "No attendee information available"}

MEETING DESCRIPTION:
{_get_event_description(event) or "No description provided"}

==========================================================
DEAL CONTEXT
==========================================================
- Deal Name: {deal.name}
- Company: {deal.company_name or "Unknown"}
- Stage: {deal.stage_id}
- Amount: {deal.amount}
- Close Date: {deal.close_date}

KNOWN STAKEHOLDERS:
{stakeholder_info or "No stakeholder information available"}
{previous_meetings_section}
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


def build_deal_summary_context_string(
    deal: CRMDeal,
    stakeholders: list[DealStakeholder],
    meetings_with_transcripts: list[dict],
) -> str:
    """Build the context string for the deal summary agent.

    This aggregates ALL meeting transcripts for a deal into a single
    context for comprehensive deal-level analysis.

    Args:
        deal: The CRM deal
        stakeholders: List of stakeholders for the deal
        meetings_with_transcripts: List of meeting dicts with 'subject', 'date',
            'attendees', and 'transcript' keys, ordered chronologically

    Returns:
        Formatted context string for the deal summary agent
    """
    # Build stakeholder section
    stakeholder_info = ""
    if stakeholders:
        stakeholder_lines = []
        for s in stakeholders:
            line = f"- {s.name}"
            if s.job_title:
                line += f" ({s.job_title})"
            if s.email:
                line += f" - {s.email}"
            if s.stance:
                line += f" | Stance: {s.stance}"
            if s.decision_power:
                line += f" | Decision Power: {s.decision_power}"
            stakeholder_lines.append(line)
        stakeholder_info = "\n".join(stakeholder_lines)

    # Build meetings section - chronologically ordered
    meetings_section = ""
    if meetings_with_transcripts:
        meeting_blocks = []
        for i, meeting in enumerate(meetings_with_transcripts, 1):
            subject = meeting.get("subject", "Unknown Subject")
            date = meeting.get("date", "Unknown Date")
            if isinstance(date, str) and "T" in date:
                date = _format_meeting_date(date)

            attendees = meeting.get("attendees", [])
            attendees_str = ""
            if attendees:
                attendees_str = "\nAttendees:\n" + "\n".join(
                    [f"  - {a.get('name', 'Unknown')} ({a.get('email', '')})" for a in attendees]
                )

            transcript = meeting.get("transcript", "No transcript available")
            # Truncate very long transcripts to avoid context overflow
            if len(transcript) > 15000:
                transcript = transcript[:15000] + "\n... [transcript truncated for brevity]"

            meeting_blocks.append(
                f"""
---
### Meeting {i}: {subject}
**Date:** {date}
{attendees_str}

**Transcript:**
{transcript}
---"""
            )

        meetings_section = "\n".join(meeting_blocks)

    # Build deal stage info
    stage_info = deal.stage_id if deal.stage_id else "Unknown"

    return f"""
==========================================================
DEAL SUMMARY REQUEST
==========================================================
Generate a comprehensive summary of this deal by analyzing ALL meeting transcripts below.

==========================================================
DEAL INFORMATION
==========================================================
- Deal Name: {deal.name}
- Company: {deal.company_name or "Unknown"}
- Deal Stage: {stage_info}
- Deal Amount: {deal.amount or "Not specified"}
- Currency: {deal.currency or "USD"}
- Close Date: {deal.close_date or "Not specified"}
- Deal Status: {deal.deal_status or "Open"}

==========================================================
KNOWN STAKEHOLDERS
==========================================================
{stakeholder_info or "No stakeholder information available"}

==========================================================
MEETING TRANSCRIPTS ({len(meetings_with_transcripts)} meetings)
==========================================================
The following transcripts are provided in CHRONOLOGICAL order (oldest first).
Analyze how the deal has progressed over time.
{meetings_section or "No meeting transcripts available"}

==========================================================
END OF CONTEXT - Generate Deal Summary
==========================================================
"""
