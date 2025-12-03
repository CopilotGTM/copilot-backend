"""Service for generating deal summaries.

This service handles the business logic for deal summary generation:
- Loading deal context and meetings
- Fetching transcripts via TranscriptService
- Building aggregated context
- Calling the DealSummaryAgent
- Saving results to CRMDeal

The Celery task delegates to this service for the actual work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.services.deal_job_utils import (
    build_deal_summary_context_string,
    get_all_deal_meetings,
    get_deal,
    get_stakeholders,
)

logger = get_logger(__name__)

# Current schema version for generated content payloads
SCHEMA_VERSION = "1.0"


class DealSummaryError(Exception):
    """Base exception for deal summary errors."""

    def __init__(self, message: str, error_code: str = "summary_error", retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable


class DealNotFoundError(DealSummaryError):
    """Raised when deal is not found."""

    def __init__(self, deal_id: UUID):
        super().__init__(
            f"Deal {deal_id} not found",
            error_code="deal_not_found",
            retryable=False,
        )


class NoMeetingsError(DealSummaryError):
    """Raised when no meetings are found for the deal."""

    def __init__(self, deal_id: UUID):
        super().__init__(
            f"No meetings found for deal {deal_id}. Schedule meetings before generating a summary.",
            error_code="no_meetings",
            retryable=False,
        )


class NoTranscriptsError(DealSummaryError):
    """Raised when no transcripts are available."""

    def __init__(self, deal_id: UUID):
        super().__init__(
            f"No meeting transcripts available for deal {deal_id}. Ensure meetings have been recorded and transcribed.",
            error_code="no_transcripts",
            retryable=False,
        )


def wrap_generated_content(content: dict) -> dict:
    """Wrap generated content with metadata envelope.

    Args:
        content: The generated content dictionary

    Returns:
        Dictionary with schema_version, generated_at, and data fields
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "data": content,
    }


class DealSummaryService:
    """Service for generating comprehensive deal summaries.

    This service orchestrates the deal summary generation process:
    1. Loads deal and stakeholder context
    2. Fetches all meetings and their transcripts
    3. Builds aggregated context for the agent
    4. Calls the DealSummaryAgent
    5. Saves results to the CRMDeal

    Usage:
        service = DealSummaryService(session, org_id, publisher)
        result = await service.generate_summary(deal_id)
    """

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        """Initialize the service.

        Args:
            session: Database session
            organisation_id: Organisation ID
            publisher: Optional Redis publisher for progress updates
        """
        self.session = session
        self.organisation_id = organisation_id
        self.publisher = publisher

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate_summary(self, deal_id: UUID) -> dict:
        """Generate a comprehensive summary for a deal.

        Analyzes all meeting transcripts associated with the deal
        to produce a holistic summary of the deal's progression.

        Args:
            deal_id: The deal ID to generate summary for

        Returns:
            Wrapped result dictionary with schema_version, generated_at, and data

        Raises:
            DealNotFoundError: If deal doesn't exist
            NoMeetingsError: If no meetings are associated with the deal
            NoTranscriptsError: If no transcripts are available
        """
        # Import here to avoid circular imports
        from src.agents.workflows.deal_summary import DealSummaryAgent

        self._publish_progress("Loading deal context...", "context", 0.1)

        # 1. Load deal
        deal = await get_deal(self.session, self.organisation_id, deal_id)
        if not deal:
            raise DealNotFoundError(deal_id)

        self._publish_progress("Loading stakeholders...", "stakeholders", 0.15)

        # 2. Load stakeholders
        stakeholders = await get_stakeholders(self.session, self.organisation_id, deal_id)

        self._publish_progress("Loading meeting history...", "meetings", 0.2)

        # 3. Load all meetings for the deal
        meetings = await get_all_deal_meetings(self.session, self.organisation_id, deal_id)
        if not meetings:
            raise NoMeetingsError(deal_id)

        self._publish_progress(
            f"Found {len(meetings)} meetings. Fetching transcripts...",
            "transcripts",
            0.25,
        )

        # 4. Fetch transcripts for each meeting
        meetings_with_transcripts = await self._fetch_transcripts(meetings)

        if not meetings_with_transcripts:
            raise NoTranscriptsError(deal_id)

        logger.info(
            "Found %d meetings with transcripts for deal %s",
            len(meetings_with_transcripts),
            deal_id,
        )

        self._publish_progress(
            f"Analyzing {len(meetings_with_transcripts)} meeting transcripts...",
            "analysis",
            0.6,
        )

        # 5. Build context string
        context_string = build_deal_summary_context_string(
            deal, stakeholders, meetings_with_transcripts
        )

        self._publish_progress("Generating deal summary...", "generation", 0.7)

        # 6. Call the agent
        agent = DealSummaryAgent()
        summary_data = await agent.generate(context_string)

        self._publish_progress("Saving results...", "saving", 0.9)

        # 7. Wrap result with metadata
        summary_dict = summary_data.model_dump()
        wrapped_result = wrap_generated_content(summary_dict)

        # 8. Save to CRMDeal for persistence
        await self._save_to_deal(deal, wrapped_result)

        return wrapped_result

    async def _fetch_transcripts(self, meetings: list) -> list[dict[str, Any]]:
        """Fetch transcripts for all meetings.

        Args:
            meetings: List of CalendarEvent objects

        Returns:
            List of meeting dicts with subject, date, attendees, and transcript
        """
        from src.service.integrations.transcript_service import TranscriptService

        transcript_service = TranscriptService(self.session, self.organisation_id)
        meetings_with_transcripts: list[dict[str, Any]] = []

        for i, event in enumerate(meetings):
            progress = 0.25 + (0.35 * (i / len(meetings)))
            self._publish_progress(
                f"Fetching transcript {i + 1}/{len(meetings)}...",
                "transcript_fetch",
                progress,
            )

            meeting_record = await transcript_service.ensure_transcript(event.event_id)
            if meeting_record and meeting_record.full_transcript:
                # Build attendees list
                attendees = []
                if event.attendees:
                    attendees = [
                        {"name": a.display_name or "Unknown", "email": a.email or ""}
                        for a in event.attendees
                    ]

                meetings_with_transcripts.append(
                    {
                        "subject": event.title or "Unknown Subject",
                        "date": event.start_at.isoformat() if event.start_at else "Unknown",
                        "attendees": attendees,
                        "transcript": meeting_record.full_transcript,
                    }
                )

        return meetings_with_transcripts

    async def _save_to_deal(self, deal: CRMDeal, result: dict) -> None:
        """Save the summary result to the CRMDeal.

        Args:
            deal: The CRM deal to update
            result: The wrapped result dictionary
        """
        deal.deal_summary_data = result
        await self.session.commit()
        logger.info("Saved deal summary to CRMDeal %s", deal.deal_id)
