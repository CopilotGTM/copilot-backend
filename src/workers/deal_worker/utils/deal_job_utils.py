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
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name

# Re-export from shared module so existing imports keep working
from src.workers.base.worker_utils import get_organisation_name

__all__ = ["get_organisation_name"]


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
) -> list[CRMDealParticipant]:
    """Load all stakeholders for a deal.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID to load stakeholders for

    Returns:
        List of contact participant entities
    """
    stmt = select(CRMDealParticipant).where(
        col(CRMDealParticipant.deal_id) == deal_id,
        col(CRMDealParticipant.organisation_id) == org_id,
        col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
        col(CRMDealParticipant.is_deleted).is_(False),
        col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
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


async def count_deal_meetings(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
) -> int:
    """Count the number of non-rejected meetings associated with a deal.

    Excludes meetings where the association status is REJECTED.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID

    Returns:
        Number of non-rejected meetings associated with the deal
    """
    from sqlalchemy import func
    from src.domain.models.organisation_tables.sync.calendar import AssociationStatus

    stmt = (
        select(func.count())
        .select_from(CalendarEventDealAssociation)
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == org_id,
            col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
        )
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_previous_meetings(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
    before_date: datetime,
) -> int:
    """Count meetings for a deal before a specific date.

    Used to determine how many previous meetings exist before
    the target meeting for briefing generation.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID
        before_date: Only count meetings before this datetime

    Returns:
        Number of meetings before the specified date
    """
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(CalendarEvent)
        .join(
            CalendarEventDealAssociation,
            col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
        )
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == org_id,
            col(CalendarEvent.start_at) < before_date,
        )
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_prior_meetings_for_briefing(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
    *,
    before_date: datetime | None,
    exclude_event_id: UUID | None,
) -> int:
    """Count prior meetings for briefing (non-rejected, excludes target event).

    This mirrors the `list_deal_meetings` tool semantics (exclude REJECTED) while
    also applying a pre-meeting cutoff (before_date) and excluding the current event.
    """

    from sqlalchemy import func
    from src.domain.models.organisation_tables.sync.calendar import AssociationStatus

    stmt = (
        select(func.count())
        .select_from(CalendarEvent)
        .join(
            CalendarEventDealAssociation,
            col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
        )
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == org_id,
            col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
        )
    )

    if exclude_event_id:
        stmt = stmt.where(col(CalendarEvent.event_id) != exclude_event_id)

    if before_date:
        stmt = stmt.where(col(CalendarEvent.start_at) < before_date)

    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_deal_stakeholders(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
) -> int:
    """Count the number of stakeholders for a deal.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID

    Returns:
        Number of stakeholders for the deal
    """
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(CRMDealParticipant)
        .where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == org_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
        )
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_deal_emails(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
) -> int:
    """Count the number of email associations for a deal.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID

    Returns:
        Number of email associations for the deal
    """
    from sqlalchemy import func
    from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation

    stmt = (
        select(func.count())
        .select_from(DealEmailAssociation)
        .where(
            col(DealEmailAssociation.deal_id) == deal_id,
            col(DealEmailAssociation.organisation_id) == org_id,
        )
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_emails_for_briefing(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
    *,
    before_date: datetime | None,
) -> int:
    """Count deal-linked emails for briefing (non-rejected, optional time cutoff).

    Uses DealEmailAssociation (same table as `list_deal_emails`).
    """

    from sqlalchemy import func
    from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
    from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation

    stmt = (
        select(func.count())
        .select_from(DealEmailAssociation)
        .where(
            col(DealEmailAssociation.deal_id) == deal_id,
            col(DealEmailAssociation.organisation_id) == org_id,
            col(DealEmailAssociation.status) == AssociationStatus.ACTIVE,
        )
    )

    if before_date:
        stmt = stmt.where(col(DealEmailAssociation.email_date) < before_date)

    result = await session.execute(stmt)
    return result.scalar() or 0


async def get_previous_emails(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
    before_date: datetime,
    limit: int = 20,
) -> list[dict]:
    """Get emails for a deal before a specific date.

    Fetches emails associated with the deal that were sent/received
    before the specified date. Used for briefing generation context.
    Does NOT include email body/snippet - the agent should use email
    tools to read full content if needed.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID
        before_date: Only include emails before this datetime
        limit: Maximum number of emails to return (default 20)

    Returns:
        List of email dictionaries with subject, datetime, direction, sender
    """
    from src.domain.models.organisation_tables.sync.email_models import (
        DealEmailAssociation,
        Email,
    )

    # Get email associations before the date
    stmt = (
        select(DealEmailAssociation)
        .where(
            col(DealEmailAssociation.deal_id) == deal_id,
            col(DealEmailAssociation.organisation_id) == org_id,
            col(DealEmailAssociation.email_date) < before_date,
        )
        .order_by(col(DealEmailAssociation.email_date).desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    associations = list(result.scalars().all())

    # Batch-fetch actual Email records for sender info
    internet_message_ids: list[str] = [
        assoc.internet_message_id for assoc in associations if assoc.internet_message_id
    ]

    email_map: dict[str, Email] = {}
    if internet_message_ids:
        email_stmt = select(Email).where(
            col(Email.organisation_id) == org_id,
            col(Email.internet_message_id).in_(internet_message_ids),
        )
        email_result = await session.execute(email_stmt)
        for e in email_result.scalars().all():
            if e.internet_message_id:
                email_map[e.internet_message_id] = e

    emails_data: list[dict] = []
    for assoc in associations:
        email = email_map.get(assoc.internet_message_id) if assoc.internet_message_id else None

        # Build sender string from Email record
        sender: str | None = None
        if email:
            sender = (
                f"{email.from_name} <{email.from_email}>" if email.from_name else email.from_email
            )

        emails_data.append(
            {
                "subject": assoc.email_subject or "No subject",
                "datetime": assoc.email_date.isoformat() if assoc.email_date else None,
                "direction": assoc.direction.value if assoc.direction else "unknown",
                "sender": sender,
            }
        )

    return emails_data


async def get_previous_meetings_with_summaries(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
    before_date: datetime,
    limit: int = 10,
) -> list[dict]:
    """Get previous meetings for a deal before a specific date.

    Fetches meetings older than the specified date that are associated with
    the deal. Returns metadata only - the agent should use meeting/transcript
    tools to read full content if needed.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID to find meetings for
        before_date: Only include meetings before this datetime
        limit: Maximum number of meetings to return (default 10)

    Returns:
        List of meeting dictionaries with event_id, subject, datetime, attendees, has_summary
    """
    from src.domain.models.organisation_tables.sync.calendar import GenerationStatus

    stmt = (
        select(CalendarEvent, CalendarEventDealAssociation)
        .options(selectinload(cast(Any, CalendarEvent.attendees)))
        .join(
            CalendarEventDealAssociation,
            col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
        )
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == org_id,
            col(CalendarEvent.start_at) < before_date,
        )
        .order_by(col(CalendarEvent.start_at).desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = list(result.all())

    meetings_data: list[dict] = []
    for event, association in rows:
        attendee_names: list[str] = []
        if event.attendees:
            attendee_names = [
                name
                for a in event.attendees
                if (name := resolve_attendee_display_name(a.display_name, a.email) or a.email)
            ]

        has_summary = (
            association is not None
            and association.meeting_summary_status == GenerationStatus.COMPLETED
            and association.meeting_summary_data is not None
        )

        meetings_data.append(
            {
                "event_id": str(event.event_id),
                "subject": event.title or "Untitled meeting",
                "datetime": event.start_at.isoformat() if event.start_at else None,
                "attendees": attendee_names,
                "has_summary": has_summary,
            }
        )

    return meetings_data

    return meetings_data


async def get_deal_meeting_date_range(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
) -> tuple[str | None, str | None]:
    """Get the date range of meetings for a deal.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID

    Returns:
        Tuple of (first_meeting_date, last_meeting_date) as ISO strings, or (None, None)
    """
    from sqlalchemy import func

    stmt = (
        select(
            func.min(CalendarEvent.start_at),
            func.max(CalendarEvent.start_at),
        )
        .select_from(CalendarEvent)
        .join(
            CalendarEventDealAssociation,
            col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
        )
        .where(
            col(CalendarEventDealAssociation.deal_id) == deal_id,
            col(CalendarEventDealAssociation.organisation_id) == org_id,
        )
    )
    result = await session.execute(stmt)
    row = result.one_or_none()

    if row and row[0] and row[1]:
        return (row[0].isoformat(), row[1].isoformat())
    return (None, None)


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
        return "\n".join(
            [
                f"- {resolve_attendee_display_name(a.display_name, a.email) or 'Unknown'} ({a.email})"
                for a in event.attendees
            ]
        )
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
    stakeholders: list[CRMDealParticipant],
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
        [
            f"- {s.related_name} ({s.job_title}): {s.stance} (Temp: {s.temperature})"
            for s in stakeholders
        ]
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
- Organizer: {resolve_attendee_display_name(event.organiser_name, event.organiser_email) or event.organiser_email or "Unknown"}
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
    stakeholders: list[CRMDealParticipant] | None = None,
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
            [f"- {s.related_name} ({s.job_title}): {s.stance}" for s in stakeholders]
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


async def count_deal_risks_blockers(
    session: AsyncSession,
    org_id: UUID,
    deal_id: UUID,
) -> dict[str, int]:
    """Count existing risks and blockers for a deal.

    Args:
        session: Database session
        org_id: Organisation ID
        deal_id: Deal ID

    Returns:
        Dictionary with 'risks', 'blockers', and 'total' counts
    """
    from sqlalchemy import func
    from src.domain.models.organisation_tables.sync.risk_blocker_models import (
        DealRiskBlocker,
        RiskBlockerStatus,
        RiskBlockerType,
    )

    # Count active (non-dismissed) items by type
    stmt = (
        select(
            col(DealRiskBlocker.item_type),
            func.count().label("count"),
        )
        .where(
            col(DealRiskBlocker.deal_id) == deal_id,
            col(DealRiskBlocker.organisation_id) == org_id,
            col(DealRiskBlocker.status) != RiskBlockerStatus.DISMISSED,
        )
        .group_by(col(DealRiskBlocker.item_type))
    )
    result = await session.execute(stmt)
    rows = result.all()

    counts: dict[str, int] = {"risks": 0, "blockers": 0, "total": 0}
    for row in rows:
        count_value = int(row[1])  # Access count by index to avoid Pylance confusion
        if row[0] == RiskBlockerType.RISK:
            counts["risks"] = count_value
        elif row[0] == RiskBlockerType.BLOCKER:
            counts["blockers"] = count_value
    counts["total"] = counts["risks"] + counts["blockers"]

    return counts
