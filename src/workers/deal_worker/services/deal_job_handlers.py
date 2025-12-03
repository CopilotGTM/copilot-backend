"""Job handlers for deal meeting tasks.

This module provides handler functions for different deal job types:
- MEETING_BRIEFING: Pre-meeting preparation briefing
- MEETING_SUMMARY: Post-meeting summary generation (requires transcript)
- MEETING_FOLLOWUP: Follow-up action items (future)
- STAKEHOLDER_ANALYSIS: Stakeholder analysis (future)
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType
from src.domain.models.organisation_tables.sync.calendar import GenerationStatus
from src.infra.utils.logger import get_logger
from src.service.integrations.transcript_service import TranscriptService
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.services.deal_job_utils import (
    build_briefing_context_string,
    build_summary_context_string,
    get_association,
    get_deal,
    get_event,
    get_previous_meetings,
    get_stakeholders,
)

logger = get_logger(__name__)

# Current schema version for generated content payloads
SCHEMA_VERSION = "1.0"


class NonRetryableJobError(Exception):
    """Error that should not trigger a job retry.

    Use this for permanent failures like:
    - No transcript available (meeting wasn't recorded)
    - Invalid meeting data
    - User-facing errors that won't resolve with retry
    """

    def __init__(self, message: str, error_code: str = "permanent_failure"):
        super().__init__(message)
        self.error_code = error_code


def wrap_generated_content(data: dict) -> dict:
    """Wrap generated content with standard metadata envelope.

    All generated content (briefing, summary, follow-up) should be wrapped
    with this envelope to provide versioning and timestamp metadata.

    Args:
        data: The generated content payload

    Returns:
        Wrapped payload with schema_version, generated_at, and data fields
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "data": data,
    }


def get_job_handler(job_type: DealJobType):
    """Get the handler function for a job type.

    Args:
        job_type: The type of job to get the handler for

    Returns:
        Handler function for the job type

    Raises:
        ValueError: If no handler exists for the job type
    """
    handlers = {
        DealJobType.MEETING_BRIEFING: handle_meeting_briefing,
        DealJobType.MEETING_SUMMARY: handle_meeting_summary,
        DealJobType.STAKEHOLDER_ANALYSIS: handle_stakeholder_analysis,
        # Add more handlers as needed
    }
    handler = handlers.get(job_type)
    if not handler:
        raise ValueError(f"No handler for job type: {job_type}")
    return handler


async def handle_meeting_briefing(
    session: AsyncSession, job: DealJobs, publisher: DealJobRedisPublisher
) -> dict:
    """Handle meeting briefing generation.

    Generates a pre-meeting briefing with context about the deal,
    stakeholders, meeting details, and transcripts from previous meetings.

    Args:
        session: Database session
        job: The deal meeting job
        publisher: Redis publisher for progress updates

    Returns:
        Dictionary containing the briefing data

    Raises:
        RuntimeError: If deal or event data is missing
    """
    from src.agents.workflows.briefing import BriefingAgent

    org_id = job.organisation_id

    publisher.publish_progress("Loading context...", "context", 0.1)

    # Load deal, event, stakeholders
    deal = await get_deal(session, org_id, job.deal_id)
    event = await get_event(session, org_id, job.event_id)
    stakeholders = await get_stakeholders(session, org_id, job.deal_id)
    association = await get_association(session, org_id, job.deal_id, job.event_id)

    if not deal or not event:
        raise RuntimeError("Missing deal or event data")

    publisher.publish_progress("Loading historical context...", "history", 0.2)

    # Fetch previous meetings for historical context
    previous_meetings = await get_previous_meetings(session, org_id, job.deal_id, event.start_at)

    # Fetch transcripts for previous meetings using TranscriptService
    previous_transcripts: list[dict] = []
    if previous_meetings:
        transcript_service = TranscriptService(session, org_id)
        for prev_event in previous_meetings:
            # ensure_transcript will fetch from provider if not in DB
            meeting = await transcript_service.ensure_transcript(prev_event.event_id)
            if meeting and meeting.full_transcript:
                previous_transcripts.append(
                    {
                        "subject": prev_event.title or "Unknown Subject",
                        "date": prev_event.start_at.isoformat()
                        if prev_event.start_at
                        else "Unknown",
                        "transcript": meeting.full_transcript,
                    }
                )

    logger.info(
        "Found %d previous meetings with transcripts for deal %s",
        len(previous_transcripts),
        job.deal_id,
    )

    publisher.publish_progress("Analyzing deal context...", "analysis", 0.3)

    # Update association status if exists
    if association:
        association.meeting_prep_status = GenerationStatus.GENERATING
        await session.commit()

    # Build context with historical transcripts and run agent
    context_string = build_briefing_context_string(
        deal, event, stakeholders, previous_transcripts if previous_transcripts else None
    )

    publisher.publish_progress("Generating briefing...", "generation", 0.5)

    agent = BriefingAgent()
    briefing_data = await agent.generate(context_string)

    publisher.publish_progress("Saving results...", "saving", 0.9)

    # Wrap result with metadata envelope and update association
    briefing_dict = briefing_data.model_dump()
    wrapped_result = wrap_generated_content(briefing_dict)
    if association:
        association.meeting_prep_data = wrapped_result
        association.meeting_prep_status = GenerationStatus.COMPLETED
        await session.commit()

    return wrapped_result


async def handle_meeting_summary(
    session: AsyncSession, job: DealJobs, publisher: DealJobRedisPublisher
) -> dict:
    """Handle meeting summary generation.

    Generates a post-meeting summary from the meeting transcript.

    Args:
        session: Database session
        job: The deal meeting job
        publisher: Redis publisher for progress updates

    Returns:
        Dictionary containing the summary data

    Raises:
        RuntimeError: If event data or transcript is missing
    """
    from src.agents.workflows.summary import SummaryAgent
    from src.domain.models.organisation_tables.sync.meeting_intelligence import MeetingStatus

    org_id = job.organisation_id
    publisher.publish_progress("Loading meeting context...", "context", 0.1)

    # Load deal and event
    deal = await get_deal(session, org_id, job.deal_id)
    event = await get_event(session, org_id, job.event_id)
    if not deal or not event:
        raise NonRetryableJobError("Missing deal or event data", "invalid_data")

    # Use TranscriptService to ensure transcript exists
    publisher.publish_progress("Checking for transcript...", "transcript", 0.2)

    transcript_service = TranscriptService(session, org_id)
    meeting = await transcript_service.ensure_transcript(job.event_id)

    if not meeting:
        # No meeting record - this is a permanent failure
        logger.info(
            "No Meeting record for event %s. Event has online_meeting_id=%s, provider=%s",
            job.event_id,
            event.online_meeting_id,
            event.online_meeting_provider,
        )
        raise NonRetryableJobError(
            "No transcript available for this meeting. The meeting may not have been recorded or transcription was not enabled.",
            "no_transcript",
        )

    if not meeting.full_transcript:
        logger.info(
            "Meeting record exists but no transcript. Status=%s, message=%s",
            meeting.status,
            meeting.status_message,
        )

        # Only retry on connection/auth failures (temporary issues)
        if meeting.status == MeetingStatus.FAILED:
            error_msg = meeting.status_message or ""
            if "connection" in error_msg.lower() or "timeout" in error_msg.lower():
                publisher.publish_progress("Connection error - will retry", "connection_error", 0.4)
                raise RuntimeError(f"Teams API connection error (retryable): {error_msg}")

        # NOT_AVAILABLE, PENDING, or other statuses - permanent failure
        if meeting.status == MeetingStatus.NOT_AVAILABLE:
            raise NonRetryableJobError(
                "No transcript available for this meeting. The meeting may not have been recorded or transcription was not enabled.",
                "no_transcript",
            )

        if meeting.status == MeetingStatus.PENDING:
            # Could be waiting for transcript processing - retry a few times
            raise RuntimeError(
                f"Transcript is still being processed. Status: {meeting.status.value}"
            )

        # Any other status - don't retry
        raise NonRetryableJobError(
            f"Transcript not available. Status: {meeting.status.value}. {meeting.status_message or ''}",
            "transcript_unavailable",
        )

    publisher.publish_progress("Loading stakeholders...", "stakeholders", 0.3)

    # Load stakeholders for context
    stakeholders = await get_stakeholders(session, org_id, job.deal_id)

    publisher.publish_progress("Analyzing transcript...", "analysis", 0.4)

    # Update association status
    association = await get_association(session, org_id, job.deal_id, job.event_id)
    if association:
        association.meeting_summary_status = GenerationStatus.GENERATING
        await session.commit()

    # Build context and run agent
    context_string = build_summary_context_string(
        deal, event, meeting.full_transcript, stakeholders
    )

    publisher.publish_progress("Generating summary...", "generation", 0.5)

    agent = SummaryAgent()
    summary_data = await agent.generate(context_string)

    publisher.publish_progress("Saving results...", "saving", 0.9)

    # Wrap result with metadata envelope and update association
    summary_dict = summary_data.model_dump()
    wrapped_result = wrap_generated_content(summary_dict)
    if association:
        association.meeting_summary_data = wrapped_result
        association.meeting_summary_status = GenerationStatus.COMPLETED
        await session.commit()

    return wrapped_result


async def handle_stakeholder_analysis(
    session: AsyncSession, job: DealJobs, publisher: DealJobRedisPublisher
) -> dict:
    """Handle stakeholder analysis for a deal.

    Analyzes stakeholders using meeting transcripts to extract insights
    about stance, decision power, motivations, and pain points.

    Args:
        session: Database session
        job: The deal meeting job
        publisher: Redis publisher for progress updates

    Returns:
        Dictionary containing the analysis results for all stakeholders

    Raises:
        NonRetryableJobError: If no stakeholders or no transcripts are available
    """
    from src.workers.deal_worker.services.stakeholder_analysis_service import (
        StakeholderAnalysisService,
    )

    org_id = job.organisation_id
    publisher.publish_progress("Loading deal context...", "context", 0.1)

    # Load deal
    deal = await get_deal(session, org_id, job.deal_id)
    if not deal:
        raise NonRetryableJobError("Deal not found", "invalid_data")

    publisher.publish_progress("Loading stakeholders...", "stakeholders", 0.2)

    # Initialize the analysis service
    analysis_service = StakeholderAnalysisService(session, org_id)

    # Get stakeholders for the deal
    stakeholders = await analysis_service.get_stakeholders_for_deal(job.deal_id)
    if not stakeholders:
        raise NonRetryableJobError(
            "No stakeholders found for this deal. Add stakeholders before running analysis.",
            "no_stakeholders",
        )

    publisher.publish_progress("Loading meeting transcripts...", "transcripts", 0.3)

    # Get meetings with transcripts
    events = await analysis_service.get_meetings_for_deal(job.deal_id)
    meetings_with_transcripts = await analysis_service.get_meetings_with_transcripts(events)

    if not meetings_with_transcripts:
        raise NonRetryableJobError(
            "No meeting transcripts available for this deal. Transcripts are needed for stakeholder analysis.",
            "no_transcripts",
        )

    publisher.publish_progress(f"Analyzing {len(stakeholders)} stakeholders...", "analysis", 0.5)

    # Batch analyze all stakeholders in one LLM call
    total_stakeholders = len(stakeholders)
    results: dict[str, dict] = {}

    try:
        analysis_results = await analysis_service.analyze_all_stakeholders_for_deal(deal)

        for stakeholder in stakeholders:
            analysis = analysis_results.get(stakeholder.stakeholder_id)
            if analysis:
                results[str(stakeholder.stakeholder_id)] = {
                    "stakeholder_name": stakeholder.name,
                    "stakeholder_email": stakeholder.email,
                    "analysis": analysis.model_dump(),
                }
            else:
                results[str(stakeholder.stakeholder_id)] = {
                    "stakeholder_name": stakeholder.name,
                    "stakeholder_email": stakeholder.email,
                    "error": "No analysis result returned",
                }

        analyzed_count = sum(1 for r in analysis_results.values() if r is not None)

    except Exception as e:
        logger.error(
            "Error in batch stakeholder analysis for deal %s: %s",
            job.deal_id,
            e,
            exc_info=True,
        )
        # Mark all stakeholders as failed
        for stakeholder in stakeholders:
            results[str(stakeholder.stakeholder_id)] = {
                "stakeholder_name": stakeholder.name,
                "stakeholder_email": stakeholder.email,
                "error": str(e),
            }
        analyzed_count = 0

    publisher.publish_progress("Saving results...", "saving", 0.9)

    await session.commit()

    # Wrap result with metadata envelope
    result_data = {
        "deal_id": str(job.deal_id),
        "deal_name": deal.name,
        "total_stakeholders": total_stakeholders,
        "analyzed_count": analyzed_count,
        "stakeholder_analyses": results,
    }

    return wrap_generated_content(result_data)
