"""
Transcript Service.

Centralized service for fetching and managing meeting transcripts.
Provider-agnostic - works with Teams, Zoom, Gong, etc.

Usage:
    service = TranscriptService(session, org_id)
    meeting = await service.ensure_transcript(event_id)
    if meeting and meeting.full_transcript:
        # Use transcript for summary generation
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingSource,
    MeetingStatus,
    TranscriptSegment,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)


class TranscriptService:
    """
    Centralized service for fetching and managing meeting transcripts.

    This service provides a simple interface to ensure transcripts exist:
    - If transcript exists in DB → return it
    - If not → fetch from provider, save, and return

    Supports multiple providers: Teams, Zoom, Gong, etc.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def ensure_transcript(
        self,
        event_id: UUID,
        force_refetch: bool = False,
    ) -> Meeting | None:
        """
        Ensure transcript exists for a calendar event.

        This is the main entry point for getting transcripts. It:
        1. Checks if Meeting record exists with READY status
        2. If exists and not force_refetch → return it
        3. If not exists or force_refetch → fetch from provider
        4. Save and return Meeting (or None if no transcript available)

        Args:
            event_id: The calendar event ID to get transcript for
            force_refetch: If True, refetch even if transcript exists

        Returns:
            Meeting with full_transcript, or None if not available
        """
        # Check for existing Meeting record
        existing = await self._get_existing_meeting(event_id)

        # Check if status is READY (handle both enum and string)
        status_str = str(existing.status) if existing else None
        if existing and status_str == MeetingStatus.READY.value and not force_refetch:
            logger.info(
                "Transcript already exists for event %s (status=%s)",
                event_id,
                str(existing.status),
            )
            return existing

        if existing:
            logger.info(
                "Meeting record exists but not READY. event=%s status=%s message=%s",
                event_id,
                str(existing.status),
                existing.status_message,
            )

        # Get the calendar event to determine provider
        event = await self._get_event(event_id)
        if not event:
            logger.warning(f"Event {event_id} not found in calendar_events table")
            return existing

        # Check if event has online meeting info
        if not event.online_meeting_id:
            logger.info(
                "Event %s has no online_meeting_id (provider=%s). Cannot fetch transcript.",
                event_id,
                event.online_meeting_provider,
            )
            return existing

        # Fetch based on provider
        if event.online_meeting_provider == "teamsForBusiness":
            return await self._fetch_teams_transcript(event, existing)
        elif event.online_meeting_provider == "zoom":
            # Zoom provider not yet implemented
            logger.info("Zoom transcripts not yet implemented for event %s", event_id)
            return existing
        elif event.online_meeting_provider == "googleMeet":
            # Google Meet doesn't provide transcript API
            logger.debug("Google Meet transcripts not available via API")
            return existing
        else:
            logger.debug(
                "Unknown provider %s for event %s", event.online_meeting_provider, event_id
            )
            return existing

    async def get_transcript_status(self, event_id: UUID) -> MeetingStatus | None:
        """
        Quick check of transcript availability without fetching.
        Used for UI indicators.

        Args:
            event_id: The calendar event ID

        Returns:
            MeetingStatus or None if no Meeting record exists
        """
        meeting = await self._get_existing_meeting(event_id)
        return meeting.status if meeting else None

    async def get_meeting_by_event(self, event_id: UUID) -> Meeting | None:
        """
        Get existing Meeting record without fetching from provider.

        Args:
            event_id: The calendar event ID

        Returns:
            Meeting or None
        """
        return await self._get_existing_meeting(event_id)

    # -------------------------------------------------------------------------
    # Private Methods
    # -------------------------------------------------------------------------

    async def _get_existing_meeting(self, event_id: UUID) -> Meeting | None:
        """Get existing Meeting record by calendar_event_id."""
        logger.info(
            "Looking up Meeting record: org_id=%s, calendar_event_id=%s",
            self.organisation_id,
            event_id,
        )
        stmt = select(Meeting).where(
            col(Meeting.organisation_id) == self.organisation_id,
            col(Meeting.calendar_event_id) == event_id,
        )
        result = await self.session.execute(stmt)
        meeting = result.scalars().first()
        if meeting:
            logger.info(
                "Found Meeting: meeting_id=%s, status=%s, has_transcript=%s",
                meeting.meeting_id,
                str(meeting.status),
                bool(meeting.full_transcript),
            )
        else:
            logger.info("No Meeting record found for calendar_event_id=%s", event_id)
        return meeting

    async def _get_event(self, event_id: UUID) -> CalendarEvent | None:
        """Get CalendarEvent by ID."""
        stmt = select(CalendarEvent).where(
            col(CalendarEvent.event_id) == event_id,
            col(CalendarEvent.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _get_integration(self, integration_id: UUID) -> KnowledgeIntegration | None:
        """Get KnowledgeIntegration by ID."""
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.integration_id) == integration_id,
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _fetch_teams_transcript(
        self,
        event: CalendarEvent,
        existing_meeting: Meeting | None,
    ) -> Meeting | None:
        """
        Fetch transcript from Microsoft Teams via Graph API.

        Args:
            event: The calendar event with online_meeting_id
            existing_meeting: Existing Meeting record to update (or None to create)

        Returns:
            Meeting with transcript, or None if fetch failed
        """
        # Get integration for connection_id
        integration = await self._get_integration(event.integration_id)
        if not integration or not integration.connection_id:
            logger.warning(f"No integration/connection found for event {event.event_id}")
            return existing_meeting

        # Create or update Meeting record with FETCHING status
        meeting = existing_meeting
        if not meeting:
            meeting = Meeting(
                organisation_id=self.organisation_id,
                calendar_event_id=event.event_id,
                source=MeetingSource.TEAMS_TRANSCRIPT,
                status=MeetingStatus.FETCHING,
            )
            self.session.add(meeting)
            await self.session.flush()
        else:
            meeting.status = MeetingStatus.FETCHING
            await self.session.flush()

        try:
            # Fetch from Teams API
            nango = NangoClient()

            # First, resolve the actual meeting ID from join URL
            # The stored online_meeting_id is the thread ID, not the API meeting ID
            actual_meeting_id = await self._resolve_teams_meeting_id(
                nango=nango,
                connection_id=integration.connection_id,
                join_url=event.conference_link,
            )

            if not actual_meeting_id:
                logger.warning(
                    "Could not resolve Teams meeting ID for event %s",
                    event.event_id,
                )
                meeting.status = MeetingStatus.NOT_AVAILABLE
                meeting.status_message = "Could not resolve Teams meeting ID"
                await self.session.commit()
                return meeting

            transcript_data = await self._call_teams_api(
                nango=nango,
                connection_id=integration.connection_id,
                online_meeting_id=actual_meeting_id,
            )

            if not transcript_data:
                # No transcript exists for this meeting (not an error)
                meeting.status = MeetingStatus.NOT_AVAILABLE
                meeting.status_message = "No transcript found for this meeting"
                await self.session.commit()
                return meeting

            # Parse and save transcript
            full_transcript, segments = self._parse_teams_transcript(
                transcript_data, meeting.meeting_id
            )

            # Update meeting record
            meeting.status = MeetingStatus.READY
            meeting.full_transcript = full_transcript
            meeting.transcript_fetched_at = datetime.now(UTC)
            meeting.transcript_language = transcript_data.get("language")
            meeting.source_data = transcript_data.get("metadata", {})

            # Delete old segments if refetching
            if existing_meeting:
                old_segments_stmt = select(TranscriptSegment).where(
                    col(TranscriptSegment.meeting_id) == meeting.meeting_id
                )
                old_result = await self.session.execute(old_segments_stmt)
                for old_seg in old_result.scalars().all():
                    await self.session.delete(old_seg)

            # Add new segments
            for segment in segments:
                self.session.add(segment)

            await self.session.commit()
            await self.session.refresh(meeting)

            logger.info(
                f"Fetched Teams transcript for event {event.event_id}: "
                f"{len(segments)} segments, {len(full_transcript)} chars"
            )
            return meeting

        except Exception as e:
            logger.error(
                f"Failed to fetch Teams transcript for event {event.event_id}: {e}",
                exc_info=True,
            )
            meeting.status = MeetingStatus.FAILED
            meeting.status_message = str(e)[:500]
            await self.session.commit()
            return meeting

    async def _resolve_teams_meeting_id(
        self,
        nango: NangoClient,
        connection_id: str,
        join_url: str | None,
        provider_config_key: str = "outlook-bflm",
    ) -> str | None:
        """
        Resolve the actual Teams meeting ID from the join URL.

        The join URL contains a thread ID (19:meeting_XXX@thread.v2), but the
        Graph API transcript endpoint needs the actual meeting ID (a GUID).

        Args:
            nango: Nango client instance
            connection_id: The OAuth connection ID
            join_url: The Teams meeting join URL
            provider_config_key: Nango provider key

        Returns:
            The actual meeting ID for Graph API, or None if not found
        """
        if not join_url:
            return None

        import urllib.parse

        # Decode URL if it's already encoded (calendar events may store encoded URLs)
        decoded_url = urllib.parse.unquote(join_url)

        # Use params dict for the filter - this lets httpx handle encoding properly
        # The filter format is: JoinWebUrl eq '{url}'
        endpoint = "/v1.0/me/onlineMeetings"
        params = {"$filter": f"JoinWebUrl eq '{decoded_url}'"}
        logger.info("Resolving Teams meeting ID from join URL")

        try:
            resp = await nango.proxy_request(
                provider_config_key=provider_config_key,
                connection_id=connection_id,
                endpoint=endpoint,
                params=params,
            )

            meetings = resp.get("value", [])
            if meetings:
                meeting_id = meetings[0].get("id")
                logger.info(
                    "Resolved Teams meeting ID: %s", meeting_id[:50] if meeting_id else None
                )
                return meeting_id

            logger.warning("No meeting found for join URL")
            return None

        except Exception as e:
            logger.warning("Failed to resolve Teams meeting ID: %s", e)
            return None

    async def _call_teams_api(
        self,
        nango: NangoClient,
        connection_id: str,
        online_meeting_id: str,
        provider_config_key: str = "outlook-bflm",
    ) -> dict | None:
        """
        Call Microsoft Graph API to fetch transcript.

        Returns dict with:
        - metadata: transcript metadata from API
        - vtt_content: raw VTT content string
        - language: transcript language

        Returns None if no transcript found.
        Raises Exception for connection/auth errors that should be retried.
        """
        import os
        import urllib.parse

        import httpx

        # URL-encode the meeting ID since it may contain special chars (* and @)
        encoded_meeting_id = urllib.parse.quote(online_meeting_id, safe="")

        # 1. List transcripts for the meeting
        endpoint = f"/v1.0/me/onlineMeetings/{encoded_meeting_id}/transcripts"
        logger.info(
            "Fetching Teams transcripts: connection_id=%s, meeting_id=%s",
            connection_id,
            online_meeting_id[:50],
        )
        try:
            resp = await nango.proxy_request(
                provider_config_key=provider_config_key,
                connection_id=connection_id,
                endpoint=endpoint,
            )
        except Exception as e:
            error_msg = str(e).lower()
            # Connection/auth errors should be raised to trigger retry
            if "connection" in error_msg or "auth" in error_msg or "token" in error_msg:
                logger.error("Teams API connection error (will retry): %s", e)
                raise RuntimeError(f"Teams API connection error: {e}") from e
            # Other errors (404, etc.) mean no transcript
            logger.warning("Failed to list transcripts from Teams API: %s", e)
            return None

        transcripts = resp.get("value", [])
        if not transcripts:
            logger.info(
                "No transcripts returned from Teams API for meeting %s",
                online_meeting_id[:50],
            )
            return None

        logger.info(
            "Found %d transcript(s) for meeting %s",
            len(transcripts),
            online_meeting_id[:50],
        )

        # Pick the first transcript
        transcript_meta = transcripts[0]
        transcript_id = transcript_meta["id"]
        encoded_transcript_id = urllib.parse.quote(transcript_id, safe="")

        # 2. Fetch VTT content (requires raw HTTP request since Nango proxy expects JSON)
        # Use $format=text/vtt query param to specify format
        content_endpoint = (
            f"/v1.0/me/onlineMeetings/{encoded_meeting_id}"
            f"/transcripts/{encoded_transcript_id}/content?$format=text/vtt"
        )

        nango_secret_key = os.getenv("NANGO_SECRET_KEY")
        if not nango_secret_key:
            logger.error("NANGO_SECRET_KEY not set")
            return None

        try:
            async with httpx.AsyncClient(
                base_url="https://api.nango.dev",
                headers={"Authorization": f"Bearer {nango_secret_key}"},
                timeout=60.0,
            ) as client:
                headers = {
                    "Provider-Config-Key": provider_config_key,
                    "Connection-Id": connection_id,
                }
                response = await client.get(f"/proxy{content_endpoint}", headers=headers)

                if response.status_code != 200:
                    error_text = response.text[:500] if response.text else "(empty)"
                    logger.warning(
                        "Failed to fetch transcript content: HTTP %s - %s",
                        response.status_code,
                        error_text,
                    )
                    return None

                vtt_content = response.text
                logger.info("Fetched VTT content: %d chars", len(vtt_content))

        except Exception as e:
            error_msg = str(e).lower()
            if "connection" in error_msg or "timeout" in error_msg:
                logger.error("Teams API connection error fetching content (will retry): %s", e)
                raise RuntimeError(f"Teams API connection error: {e}") from e
            logger.warning("Failed to fetch transcript content: %s", e)
            return None

        return {
            "metadata": transcript_meta,
            "vtt_content": vtt_content,
            "language": transcript_meta.get("language"),
        }

    def _parse_teams_transcript(
        self,
        transcript_data: dict,
        meeting_id: UUID,
    ) -> tuple[str, list[TranscriptSegment]]:
        """
        Parse Teams transcript VTT data into full_transcript and segments.

        VTT format example:
            WEBVTT

            0:0:0.0 --> 0:0:5.320
            <v Speaker Name>spoken text</v>

        Args:
            transcript_data: Dict with metadata and vtt_content from Teams API
            meeting_id: The Meeting ID to link segments to

        Returns:
            Tuple of (full_transcript_text, list of TranscriptSegment)
        """
        import re

        vtt_content = transcript_data.get("vtt_content", "")
        full_transcript_parts: list[str] = []
        segments: list[TranscriptSegment] = []

        lines = vtt_content.split("\n")
        segment_idx = 0

        for line in lines:
            line = line.strip()
            # Skip header, timestamps, and empty lines
            if not line or line == "WEBVTT" or "-->" in line:
                continue

            # Parse VTT speaker format: <v Speaker Name>spoken text</v>
            match = re.match(r"<v ([^>]+)>(.+)</v>", line)
            if match:
                speaker_name = match.group(1).strip()
                text_content = match.group(2).strip()

                # Build full transcript line
                full_transcript_parts.append(f"{speaker_name}: {text_content}")

                # Create segment
                segment = TranscriptSegment(
                    meeting_id=meeting_id,
                    speaker_external_id=None,  # VTT doesn't include speaker ID
                    speaker_name=speaker_name,
                    segment_index=segment_idx,
                    content=text_content,
                )
                segments.append(segment)
                segment_idx += 1
            elif line and not line.startswith("NOTE"):
                # Plain text line (no speaker tags)
                full_transcript_parts.append(line)
                segment = TranscriptSegment(
                    meeting_id=meeting_id,
                    speaker_external_id=None,
                    speaker_name="Unknown",
                    segment_index=segment_idx,
                    content=line,
                )
                segments.append(segment)
                segment_idx += 1

        full_transcript = "\n".join(full_transcript_parts)
        return full_transcript, segments
