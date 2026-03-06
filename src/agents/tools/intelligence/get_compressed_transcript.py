"""Get compressed meeting transcript tool for agents.

Fetches transcript segments from DB, then uses TranscriptCompressionAgent
to condense them into speaker-attributed key points. The raw transcript
never enters the parent agent's context — only the compressed output.

SECURITY: organisation_id is taken from context (immutable).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.agents.sub_agents.transcript_compression import TranscriptCompressionAgent
from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.service.integrations.transcripts import TranscriptService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _format_segments_for_compression(segments: list[dict[str, Any]]) -> str:
    """Format transcript segments into a readable speaker-labeled text.

    Args:
        segments: List of segment dicts with speaker_name, content, start_time_seconds.

    Returns:
        Formatted string like "Speaker Name [00:03:15]: content\\n..."
    """
    lines = []
    for seg in segments:
        speaker = seg.get("speaker_name") or "Unknown Speaker"
        content = (seg.get("content") or "").strip()
        if not content:
            continue

        start_seconds = seg.get("start_time_seconds")
        if start_seconds is not None:
            minutes, secs = divmod(int(start_seconds), 60)
            hours, minutes = divmod(minutes, 60)
            timestamp = f"[{hours:02d}:{minutes:02d}:{secs:02d}]"
        else:
            timestamp = ""

        lines.append(f"{speaker} {timestamp}: {content}")

    return "\n".join(lines)


async def get_compressed_transcript(
    event_id: str,
    *,
    context: AgentContext,
    org_name: str = "Our Organization",
) -> dict[str, Any] | None:
    """Fetch transcript segments, compress via sub-agent, return condensed output.

    Workflow:
    1. Ensure transcript exists (fetch from provider if needed)
    2. Load transcript segments from DB (speaker-attributed)
    3. Release DB session
    4. Run TranscriptCompressionAgent to condense
    5. Return compressed result

    SECURITY: organisation_id comes from context, not parameters.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        logger.warning("get_compressed_transcript: missing organisation_id in context")
        return None

    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        logger.warning("get_compressed_transcript: invalid event_id: %s", event_id)
        return None

    # Step 1: Ensure transcript exists
    transcript_service = TranscriptService(organisation_id)
    try:
        await transcript_service.ensure_transcript(parsed_event_id)
    except Exception as e:
        logger.warning("get_compressed_transcript: ensure_transcript failed: %s", e)

    # Step 2: Fetch meeting with segments (fresh session)
    meeting_c = cast(Any, Meeting).__table__.c
    event_c = cast(Any, CalendarEvent).__table__.c

    segments_data: list[dict[str, Any]] = []
    title: str | None = None
    meeting_date: str | None = None

    async with context.acquire_session() as session:
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
            result = await session.execute(meeting_stmt)
            meeting = result.scalar_one_or_none()
        except Exception as e:
            logger.exception("get_compressed_transcript: error fetching meeting: %s", e)
            return None

        if not meeting:
            logger.info("get_compressed_transcript: no meeting found for event: %s", event_id)
            return None

        if meeting.status != MeetingStatus.READY:
            return {
                "event_id": event_id,
                "transcript_available": False,
                "status": (
                    meeting.status.value
                    if isinstance(meeting.status, MeetingStatus)
                    else meeting.status
                ),
            }

        # Extract segments while session is active
        for seg in sorted(meeting.transcript_segments, key=lambda s: s.segment_index):
            segments_data.append(
                {
                    "speaker_name": seg.speaker_name,
                    "content": seg.content,
                    "start_time_seconds": seg.start_time_seconds,
                }
            )

        # Fetch event for title/date
        event_stmt = select(CalendarEvent).where(event_c.event_id == parsed_event_id)
        try:
            event_result = await session.execute(event_stmt)
            event = event_result.scalar_one_or_none()
            if event:
                title = event.title
                meeting_date = event.start_at.date().isoformat() if event.start_at else None
        except Exception as e:
            logger.warning("get_compressed_transcript: error fetching event: %s", e)

    # Session released — safe to call sub-agent

    if not segments_data:
        logger.info("get_compressed_transcript: no segments for event %s", event_id)
        return {
            "event_id": event_id,
            "title": title,
            "transcript_available": True,
            "compressed_transcript": "(No transcript segments available)",
        }

    # Step 3: Format and compress
    formatted_text = _format_segments_for_compression(segments_data)
    safe_title = title or "Untitled Meeting"
    safe_date = meeting_date or "Unknown Date"

    compression_agent = TranscriptCompressionAgent(org_name=org_name)
    compressed = await compression_agent.compress(safe_title, safe_date, formatted_text)

    # Approximate token counts (1 token ≈ 4 chars for English text)
    input_tokens = len(formatted_text) // 4
    output_tokens = len(compressed) // 4
    reduction_pct = round((1 - output_tokens / input_tokens) * 100, 1) if input_tokens else 0

    logger.info(
        "get_compressed_transcript [%s]: ~%d tokens in → ~%d tokens out (%.1f%% reduction)",
        event_id,
        input_tokens,
        output_tokens,
        reduction_pct,
    )

    return {
        "event_id": event_id,
        "title": title,
        "meeting_date": meeting_date,
        "transcript_available": True,
        "compressed_transcript": compressed,
        "original_segment_count": len(segments_data),
        "token_estimate_input": input_tokens,
        "token_estimate_output": output_tokens,
        "compression_ratio": f"{reduction_pct}%",
    }


def create_get_compressed_transcript(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_compressed_transcript tool bound to a context.

    SECURITY: The context contains the immutable organisation_id.
    """

    async def get_compressed_transcript_tool(
        event_id: str,
    ) -> dict[str, Any] | None:
        """Get a compressed, speaker-attributed summary of a meeting transcript.

        Use this tool when you need to understand what was discussed in a meeting.
        Returns a condensed version with key dialogue points attributed to speakers,
        plus decisions, action items, and unresolved items.

        Much more efficient than reading the raw transcript — use this as your
        primary way to access meeting content.

        Args:
            event_id: The calendar event UUID (from list_deal_meetings)

        Note: organisation_id is automatically set from context and cannot be overridden.
        """
        logger.info(
            "[TOOL CALLED] get_compressed_transcript - event_id='%s'",
            event_id,
        )

        if not context.organisation_id:
            logger.warning("get_compressed_transcript unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetCompressedTranscript",
            tool_func=get_compressed_transcript,
            parameters={
                "event_id": event_id,
                "context": context,
                "org_name": context.org_name,
            },
            description=f"Compressing transcript for meeting: {event_id}",
            fallback_value=None,
        )

        if result:
            available = result.get("transcript_available", False)
            logger.info("[TOOL RESULT] get_compressed_transcript - available=%s", available)
        else:
            logger.info("[TOOL RESULT] get_compressed_transcript returned None")

        return dict(result) if result else None

    return get_compressed_transcript_tool
