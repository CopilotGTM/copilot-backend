"""Get meeting transcript tool for agents.

Tool for retrieving the transcript of a specific meeting.
Integrates with TranscriptService to automatically fetch transcripts
from providers (Teams, Zoom, etc.) if not already available.

SECURITY: organisation_id is taken from context (immutable).
This tool does NOT accept organisation_id as a parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingSource,
    MeetingStatus,
)
from src.service.integrations.transcripts import TranscriptService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_meeting_transcript(
    event_id: str,
    *,
    context: AgentContext,
    output_format: str = "full",
    max_length: int | None = None,
) -> dict[str, Any] | None:
    """Get the transcript for a specific meeting.

    This tool integrates with TranscriptService to automatically fetch transcripts
    from providers (Teams, Zoom, etc.) if not already available in the database.

    SECURITY: organisation_id is taken from context (immutable).
    This prevents cross-org data access.

    Workflow:
    1. Call TranscriptService.ensure_transcript() to check/fetch transcript
    2. Read and return the transcript data (using fresh session)

    Args:
        event_id: The calendar event UUID as a string
        context: AgentContext containing organisation metadata
        output_format: "full" for concatenated text, "segments" for structured utterances
        max_length: Truncate to N characters (None = no limit)

    Returns:
        Transcript dictionary, or None if not found/available.
    """
    # SECURITY: organisation_id comes from context, not parameters
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_meeting_transcript: missing organisation_id in context")
        return None

    # Parse event_id
    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        logger.warning(f"get_meeting_transcript: invalid event_id format: {event_id}")
        return None

    # Validate output_format
    if output_format not in ("full", "segments"):
        logger.warning(f"get_meeting_transcript: invalid format: {output_format}, using 'full'")
        output_format = "full"

    # -------------------------------------------------------------------
    # STEP 1: Ensure transcript exists (fetch from provider if needed)
    # -------------------------------------------------------------------
    # TranscriptService will check if transcript is already in DB,
    # and if not, attempt to fetch from the provider (Teams, Zoom, etc.)
    # TranscriptService manages its own sessions
    transcript_service = TranscriptService(organisation_id)
    try:
        ensured_meeting = await transcript_service.ensure_transcript(parsed_event_id)
        if ensured_meeting:
            logger.info(
                f"get_meeting_transcript: ensure_transcript returned meeting with status={ensured_meeting.status}"
            )
    except Exception as e:
        # Log but continue - we'll try to read whatever is in DB
        logger.warning(f"get_meeting_transcript: ensure_transcript failed: {e}")

    # -------------------------------------------------------------------
    # STEP 2: Fetch meeting with transcript data (fresh session)
    # -------------------------------------------------------------------
    # Use table columns for SQLAlchemy operations (type-safe pattern)
    meeting_c = cast(Any, Meeting).__table__.c
    event_c = cast(Any, CalendarEvent).__table__.c

    meeting: Meeting | None = None
    event: CalendarEvent | None = None

    # Acquire fresh session for database read
    async with context.acquire_session() as session:
        # Always eager-load segments — we fall back to full_transcript if empty
        meeting_stmt = (
            select(Meeting)
            .options(selectinload(cast(Any, Meeting).transcript_segments))
            .where(
                meeting_c.organisation_id == organisation_id,
                meeting_c.calendar_event_id == parsed_event_id,
                meeting_c.is_deleted.is_(False),
            )
        )

        try:
            meeting_result = await session.execute(meeting_stmt)
            meeting = meeting_result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_meeting_transcript: error fetching meeting: {e}")
            return None

        if not meeting:
            logger.info(f"get_meeting_transcript: no meeting found for event: {event_id}")
            return None

        if meeting.status != MeetingStatus.READY:
            logger.info(f"get_meeting_transcript: transcript not ready, status: {meeting.status}")
            return {
                "event_id": event_id,
                "transcript_available": False,
                "status": meeting.status.value
                if isinstance(meeting.status, MeetingStatus)
                else meeting.status,
                "status_message": meeting.status_message,
            }

        # Fetch event for title/date info
        event_stmt = select(CalendarEvent).where(event_c.event_id == parsed_event_id)

        try:
            event_result = await session.execute(event_stmt)
            event = event_result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_meeting_transcript: error fetching event: {e}")
            event = None

        # Build output (while session is still active for segments access)
        output: dict[str, Any] = {
            "event_id": event_id,
            "title": event.title if event else None,
            "meeting_date": event.start_at.date().isoformat() if event and event.start_at else None,
            "duration_seconds": meeting.duration_seconds,
            "transcript_available": True,
            "source": meeting.source.value
            if isinstance(meeting.source, MeetingSource)
            else meeting.source,
            "language": meeting.transcript_language,
        }

        if output_format == "full":
            full_text = meeting.full_transcript or ""
            total_length = len(full_text)

            if max_length and total_length > max_length:
                output["full_text"] = full_text[:max_length]
                output["truncated"] = True
            else:
                output["full_text"] = full_text
                output["truncated"] = False

            output["total_length"] = total_length

        else:  # output_format == "segments"
            segments = []
            for seg in sorted(meeting.transcript_segments, key=lambda s: s.segment_index):
                segments.append(
                    {
                        "index": seg.segment_index,
                        "speaker_name": seg.speaker_name,
                        "speaker_external_id": seg.speaker_external_id,
                        "content": seg.content,
                        "start_time_seconds": seg.start_time_seconds,
                        "end_time_seconds": seg.end_time_seconds,
                        "confidence": float(seg.confidence) if seg.confidence else None,
                    }
                )

            if segments:
                output["segments"] = segments
                output["segment_count"] = len(segments)
                output["truncated"] = False
            else:
                # Segments missing (unsupported note-taker format) — fall back
                # to full_transcript so the caller still gets usable data.
                full_text = meeting.full_transcript or ""
                total_length = len(full_text)
                if max_length and total_length > max_length:
                    output["full_text"] = full_text[:max_length]
                    output["truncated"] = True
                else:
                    output["full_text"] = full_text
                    output["truncated"] = False
                output["total_length"] = total_length
                output["segments"] = []
                output["segment_count"] = 0
                output["fell_back_to_full_transcript"] = True
    # Session released

    logger.info(f"get_meeting_transcript: returning transcript for event {event_id}")
    return output


def create_get_meeting_transcript(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_meeting_transcript tool bound to a context.

    SECURITY: The context contains the immutable organisation_id.
    The tool does NOT accept organisation_id as a parameter.
    """

    async def get_meeting_transcript_tool(
        event_id: str,
        output_format: str = "full",
        max_length: int | None = None,
    ) -> dict[str, Any] | None:
        """Get the transcript for a specific meeting.

        Use this tool when you need to analyze what was actually said in a meeting.
        If the transcript is not yet downloaded, this tool will automatically
        attempt to fetch it from the provider (Teams, Zoom, etc.).

        Args:
            event_id: The calendar event UUID (from list_deal_meetings)
            output_format: "full" for concatenated text, "segments" for speaker-attributed utterances
            max_length: Truncate to N characters (default: no limit)

        Note: organisation_id is automatically set from context and cannot be overridden.
        """
        logger.info(
            f"[TOOL CALLED] get_meeting_transcript - event_id='{event_id}', "
            f"output_format='{output_format}', max_length={max_length}"
        )

        if not context.organisation_id:
            logger.warning("get_meeting_transcript unavailable: missing organisation_id in context")
            return None

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetMeetingTranscript",
            tool_func=get_meeting_transcript,
            parameters={
                "event_id": event_id,
                "context": context,
                "output_format": output_format,
                "max_length": max_length,
            },
            description="Reading meeting transcript",
            fallback_value=None,
        )

        if result:
            available = result.get("transcript_available", False)
            logger.info(f"[TOOL RESULT] get_meeting_transcript - available={available}")
        else:
            logger.info("[TOOL RESULT] get_meeting_transcript returned None (not found)")

        return dict(result) if result else None

    return get_meeting_transcript_tool
