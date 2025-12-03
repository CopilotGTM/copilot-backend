"""Stakeholder analysis service for processing stakeholder insights.

This module provides a service for analyzing stakeholders using meeting transcripts
to extract insights about stance, decision power, motivations, and pain points.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.agents.workflows.stakeholder_analysis import (
    DealStakeholdersAnalysisData,
    StakeholderAnalysisAgent,
    StakeholderAnalysisData,
)
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting
from src.infra.utils.logger import get_logger
from src.service.integrations.transcript_service import TranscriptService

logger = get_logger(__name__)


class StakeholderAnalysisService:
    """Service for analyzing stakeholders from meeting transcripts.

    This service:
    1. Fetches all meetings associated with a deal
    2. Filters transcripts where the stakeholder participated
    3. Builds context for the analysis agent
    4. Updates stakeholder records with analysis results
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        """Initialize the service.

        Args:
            session: Async database session for the organisation
            organisation_id: UUID of the organisation
        """
        self.session = session
        self.organisation_id = organisation_id

    async def get_stakeholders_for_deal(self, deal_id: UUID) -> list[DealStakeholder]:
        """Get all stakeholders for a deal.

        Args:
            deal_id: UUID of the deal

        Returns:
            List of DealStakeholder objects
        """
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.deal_id) == deal_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_all_deals_with_stakeholders(self) -> list[CRMDeal]:
        """Get all deals that have stakeholders.

        Returns:
            List of CRMDeal objects that have at least one stakeholder
        """
        # Get deal IDs that have stakeholders using a subquery
        stakeholder_subq = (
            select(DealStakeholder)
            .where(col(DealStakeholder.organisation_id) == self.organisation_id)
            .subquery()
        )

        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.deal_id).in_(select(stakeholder_subq.c.deal_id).distinct()),
            col(CRMDeal.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_meetings_for_deal(self, deal_id: UUID) -> list[CalendarEvent]:
        """Get all calendar events associated with a deal that have transcripts.

        Args:
            deal_id: UUID of the deal

        Returns:
            List of CalendarEvent objects linked to the deal
        """
        attendees_rel = cast(Any, CalendarEvent.attendees)
        stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_rel))
            .join(
                CalendarEventDealAssociation,
                col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
            )
            .where(
                col(CalendarEventDealAssociation.deal_id) == deal_id,
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            )
            .order_by(col(CalendarEvent.start_at).desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_meetings_with_transcripts(
        self, events: list[CalendarEvent]
    ) -> list[tuple[CalendarEvent, Meeting]]:
        """Get meetings that have transcripts available, fetching from provider if needed.

        Uses TranscriptService.ensure_transcript() to fetch transcripts from the
        provider (Teams, Zoom, etc.) if they're not already in the database.

        Args:
            events: List of CalendarEvent objects to get transcripts for

        Returns:
            List of tuples (CalendarEvent, Meeting) with available transcripts
        """
        if not events:
            return []

        transcript_service = TranscriptService(self.session, self.organisation_id)
        results: list[tuple[CalendarEvent, Meeting]] = []

        for event in events:
            meeting = await transcript_service.ensure_transcript(event.event_id)
            if meeting and meeting.full_transcript:
                results.append((event, meeting))
            else:
                logger.debug(
                    "No transcript available for event %s (%s)",
                    event.event_id,
                    event.title,
                )

        return results

    def _format_meeting_transcript(self, index: int, event: CalendarEvent, meeting: Meeting) -> str:
        """Format a single meeting transcript for the context.

        Args:
            index: Meeting index (1-based)
            event: The calendar event
            meeting: The meeting with transcript

        Returns:
            Formatted transcript section
        """
        meeting_date = (
            event.start_at.strftime("%Y-%m-%d %H:%M") if event.start_at else "Unknown date"
        )
        subject = event.title or "Unknown Subject"

        # Get attendees list
        attendees_str = "Unknown"
        if event.attendees:
            attendees = [f"{a.display_name or 'Unknown'} ({a.email})" for a in event.attendees]
            attendees_str = ", ".join(attendees)

        transcript = meeting.full_transcript or "No transcript available"
        # Truncate very long transcripts
        if len(transcript) > 15000:
            transcript = transcript[:15000] + "\n... [transcript truncated]"

        return f"""
### Meeting {index}: {subject}
**Date:** {meeting_date}
**Attendees:** {attendees_str}

**Transcript:**
{transcript}
"""

    def _get_stakeholder_info(self, stakeholder: DealStakeholder) -> dict[str, str]:
        """Extract stakeholder info for context building.

        Args:
            stakeholder: The stakeholder

        Returns:
            Dictionary with stakeholder info
        """
        stance_val = stakeholder.stance.value if stakeholder.stance else "UNKNOWN"
        power_val = stakeholder.decision_power.value if stakeholder.decision_power else "UNKNOWN"
        temp_val = stakeholder.temperature.value if stakeholder.temperature else "COLD"
        last_contact = (
            stakeholder.last_contact_at.strftime("%Y-%m-%d")
            if stakeholder.last_contact_at
            else "Never"
        )

        return {
            "name": stakeholder.name or "Unknown",
            "email": stakeholder.email,
            "job_title": stakeholder.job_title or "Unknown",
            "department": stakeholder.department or "Unknown",
            "stance": stance_val,
            "decision_power": power_val,
            "temperature": temp_val,
            "engagement_score": str(stakeholder.engagement_score),
            "last_contact": last_contact,
        }

    def build_batch_analysis_context(
        self,
        stakeholders: list[DealStakeholder],
        deal: CRMDeal,
        meetings: list[tuple[CalendarEvent, Meeting]],
    ) -> str:
        """Build the context string for batch stakeholder analysis.

        Args:
            stakeholders: List of stakeholders to analyze
            deal: The deal context
            meetings: List of (event, meeting) tuples with transcripts

        Returns:
            Formatted context string for batch analysis
        """
        # Format meeting transcripts
        transcripts_section = [
            self._format_meeting_transcript(i, event, meeting)
            for i, (event, meeting) in enumerate(meetings, 1)
        ]

        # Format all stakeholders info
        stakeholders_section = []
        for idx, stakeholder in enumerate(stakeholders, 1):
            info = self._get_stakeholder_info(stakeholder)
            stakeholders_section.append(f"""
### Stakeholder {idx}:
- **Email (ID):** {info["email"]}
- Name: {info["name"]}
- Job Title: {info["job_title"]}
- Department: {info["department"]}
- Current Stance: {info["stance"]}
- Current Decision Power: {info["decision_power"]}
- Current Temperature: {info["temperature"]}
- Engagement Score: {info["engagement_score"]}
- Last Contact: {info["last_contact"]}
""")

        return f"""
==========================================================
BATCH STAKEHOLDER ANALYSIS REQUEST
==========================================================

Analyze ALL of the following stakeholders from this deal based on the meeting transcripts.
Return analysis for EACH stakeholder using their email as the identifier.

==========================================================
STAKEHOLDERS TO ANALYZE ({len(stakeholders)} total)
==========================================================
{chr(10).join(stakeholders_section)}

==========================================================
DEAL CONTEXT
==========================================================
- Deal Name: {deal.name}
- Company: {deal.company_name or "Unknown"}
- Stage: {deal.stage_id}
- Amount: {deal.amount}
- Close Date: {deal.close_date}

==========================================================
MEETING TRANSCRIPTS
==========================================================
The following are ALL meeting transcripts associated with this deal.
Analyze each stakeholder based on their participation in these meetings.
Look for each stakeholder by their email address in the attendee lists.

{chr(10).join(transcripts_section) if transcripts_section else "No meeting transcripts available."}
"""

    def update_stakeholder_with_analysis(
        self, stakeholder: DealStakeholder, analysis: StakeholderAnalysisData
    ) -> None:
        """Update stakeholder record with analysis results.

        Args:
            stakeholder: The stakeholder to update
            analysis: The analysis results
        """
        stakeholder.stance = analysis.stance
        stakeholder.decision_power = analysis.decision_power
        stakeholder.temperature = analysis.temperature
        stakeholder.champion_potential = analysis.champion_potential
        stakeholder.motivation = analysis.motivation if analysis.motivation else None
        stakeholder.pain_points = analysis.pain_points if analysis.pain_points else None
        stakeholder.keywords = analysis.keywords if analysis.keywords else None

        logger.info(
            "Updated stakeholder %s with analysis: stance=%s, decision_power=%s, temperature=%s",
            stakeholder.stakeholder_id,
            analysis.stance,
            analysis.decision_power,
            analysis.temperature,
        )

    async def analyze_all_stakeholders_for_deal(
        self, deal: CRMDeal
    ) -> dict[UUID, StakeholderAnalysisData | None]:
        """Analyze all stakeholders for a deal in a single LLM call.

        This method batches all stakeholders for a deal and analyzes them
        together for efficiency and better contextual understanding.

        Args:
            deal: The deal to analyze stakeholders for

        Returns:
            dict[UUID, StakeholderAnalysisData | None]: A dictionary mapping each stakeholder's UUID
                to their analysis results. If no analysis is available for a stakeholder (e.g., no
                relevant meetings or transcripts), the value will be None.
        """

        stakeholders = await self.get_stakeholders_for_deal(deal.deal_id)
        if not stakeholders:
            logger.info("No stakeholders found for deal %s", deal.deal_id)
            return {}

        # Get all meetings for the deal
        events = await self.get_meetings_for_deal(deal.deal_id)
        if not events:
            logger.info("No meetings found for deal %s, skipping analysis", deal.deal_id)
            return {s.stakeholder_id: None for s in stakeholders}

        # Get meetings with transcripts
        meetings_with_transcripts = await self.get_meetings_with_transcripts(events)
        if not meetings_with_transcripts:
            logger.info("No transcripts available for deal %s, skipping analysis", deal.deal_id)
            return {s.stakeholder_id: None for s in stakeholders}

        # Build context for all stakeholders
        context = self.build_batch_analysis_context(stakeholders, deal, meetings_with_transcripts)

        logger.info(
            "Running batch analysis for %d stakeholders in deal %s with %d meetings",
            len(stakeholders),
            deal.deal_id,
            len(meetings_with_transcripts),
        )

        # Run batch analysis
        agent = StakeholderAnalysisAgent()
        batch_result: DealStakeholdersAnalysisData = await agent.generate(context)

        # Map results by email to stakeholders
        email_to_analysis: dict[str, StakeholderAnalysisData] = {
            a.stakeholder_email.lower(): a for a in batch_result.stakeholders
        }

        results: dict[UUID, StakeholderAnalysisData | None] = {}
        for stakeholder in stakeholders:
            analysis = email_to_analysis.get(stakeholder.email.lower())
            if analysis:
                self.update_stakeholder_with_analysis(stakeholder, analysis)
                results[stakeholder.stakeholder_id] = analysis
            else:
                logger.warning(
                    "No analysis result for stakeholder %s (%s)",
                    stakeholder.stakeholder_id,
                    stakeholder.email,
                )
                results[stakeholder.stakeholder_id] = None

        return results

    async def analyze_deal_by_id(self, deal_id: UUID) -> dict[str, int]:
        """Analyze all stakeholders for a deal by its ID.

        Args:
            deal_id: UUID of the deal to analyze

        Returns:
            Dictionary with processing statistics
        """
        # Load deal
        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        deal_result = await self.session.execute(deal_stmt)
        deal = deal_result.scalars().first()

        if not deal:
            logger.warning("Deal %s not found in org %s", deal_id, self.organisation_id)
            return {
                "stakeholders_processed": 0,
                "stakeholders_analyzed": 0,
                "stakeholders_skipped": 0,
            }

        results = await self.analyze_all_stakeholders_for_deal(deal)
        await self.session.commit()

        analyzed = sum(1 for r in results.values() if r is not None)
        skipped = sum(1 for r in results.values() if r is None)

        return {
            "stakeholders_processed": len(results),
            "stakeholders_analyzed": analyzed,
            "stakeholders_skipped": skipped,
        }

    async def analyze_all_stakeholders_for_organisation(self) -> dict[str, int]:
        """Analyze all stakeholders across all deals in the organisation.

        Each deal's stakeholders are analyzed in a single batch LLM call
        for efficiency.

        Returns:
            Dictionary with processing statistics
        """
        stats = {
            "deals_processed": 0,
            "stakeholders_processed": 0,
            "stakeholders_analyzed": 0,
            "stakeholders_skipped": 0,
        }

        deals = await self.get_all_deals_with_stakeholders()
        logger.info(
            "Processing stakeholder analysis for %d deals in org %s",
            len(deals),
            self.organisation_id,
        )

        for deal in deals:
            stats["deals_processed"] += 1
            try:
                results = await self.analyze_all_stakeholders_for_deal(deal)
                stats["stakeholders_processed"] += len(results)
                stats["stakeholders_analyzed"] += sum(1 for r in results.values() if r is not None)
                stats["stakeholders_skipped"] += sum(1 for r in results.values() if r is None)
            except Exception as e:
                logger.error(
                    "Error analyzing stakeholders for deal %s: %s",
                    deal.deal_id,
                    e,
                    exc_info=True,
                )
                # Count all stakeholders as skipped on error
                stakeholder_count = len(await self.get_stakeholders_for_deal(deal.deal_id))
                stats["stakeholders_processed"] += stakeholder_count
                stats["stakeholders_skipped"] += stakeholder_count

        await self.session.commit()
        return stats
