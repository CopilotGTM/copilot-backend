"""Transcript Service - orchestrates transcript fetching across providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingSource,
    MeetingStatus,
    TranscriptSegment,
)
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingTranscriptStatus,
)
from src.infra.config.microsoft_integration_scopes import (
    MICROSOFT_SCOPE_ONLINE_MEETING_TRANSCRIPT_READ_ALL,
    MICROSOFT_SCOPE_ONLINE_MEETINGS_READ,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.automation.producers.meeting import (
    MeetingEventAdapter,
    load_meeting_event_context,
)
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.meeting_bot.providers import (
    RecallMeetingRecordingProvider,
    RecallProviderError,
)

from .google_meet_provider import GoogleMeetTranscriptProvider, TranscriptAuthError
from .teams_provider import TeamsTranscriptProvider

logger = get_logger(__name__)

_TEAMS_TRANSCRIPT_SCOPES = frozenset(
    {
        MICROSOFT_SCOPE_ONLINE_MEETINGS_READ,
        MICROSOFT_SCOPE_ONLINE_MEETING_TRANSCRIPT_READ_ALL,
    }
)
# Transcript APIs are only available through the parent Google/Microsoft grants.
# Calendar-only child integrations can prove event ownership but cannot fetch text.
_CALENDAR_PROVIDER_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
)
_MEETING_PROVIDER_INTEGRATION_TYPES = {
    "googleMeet": IntegrationType.GOOGLE,
    "teamsForBusiness": IntegrationType.MICROSOFT,
}
_COMPATIBLE_CALENDAR_INTEGRATION_TYPES = {
    IntegrationType.GOOGLE: {IntegrationType.GOOGLE, IntegrationType.GOOGLE_CALENDAR},
    IntegrationType.MICROSOFT: {IntegrationType.MICROSOFT, IntegrationType.OUTLOOK_CALENDAR},
}


def _has_teams_transcript_scopes(integration: KnowledgeIntegration) -> bool:
    return _TEAMS_TRANSCRIPT_SCOPES.issubset(set(integration.scopes or []))


def _normalize_integration_type(value: IntegrationType | str | None) -> IntegrationType | None:
    if isinstance(value, IntegrationType):
        return value
    if not value:
        return None
    try:
        return IntegrationType(value)
    except ValueError:
        return None


def _expected_provider_integration_type(event: CalendarEvent) -> IntegrationType | None:
    return _MEETING_PROVIDER_INTEGRATION_TYPES.get(event.online_meeting_provider or "")


def _compatible_calendar_integration_types(
    provider_type: IntegrationType | None,
) -> tuple[IntegrationType, ...]:
    if provider_type is None:
        return _CALENDAR_PROVIDER_INTEGRATION_TYPES
    return (provider_type,)


def _integration_matches_event_provider(
    event: CalendarEvent,
    integration: KnowledgeIntegration,
) -> bool:
    expected = _expected_provider_integration_type(event)
    if expected is None:
        return True
    actual = _normalize_integration_type(integration.integration_type)
    return actual in _COMPATIBLE_CALENDAR_INTEGRATION_TYPES[expected]


@dataclass
class TranscriptFetchResult:
    """Result of a transcript fetch attempt."""

    success: bool
    meeting: Meeting | None = None
    error: str | None = None
    should_retry: bool = False


@dataclass
class TranscriptFetchStatus:
    """Result of fetching transcript for an event with retry tracking."""

    status: str  # "fetched", "pending", "not_available", "failed"
    integrations_tried: int = 0
    fetched_with: str | None = None
    error: str | None = None


class TranscriptService:
    """Centralized service for fetching and managing meeting transcripts.

    Provider-agnostic orchestration - delegates to Teams/Zoom/Gong providers.

    Session Handling:
    - Acquires session only when needed for DB operations
    - Releases immediately after each operation
    - Never holds session during external API calls
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self._teams = TeamsTranscriptProvider()
        self._google_meet = GoogleMeetTranscriptProvider()
        self._enable_recall_transcripts = True

    def _build_token_info(self, integration: KnowledgeIntegration) -> IntegrationTokenInfo:
        return IntegrationTokenInfo.from_integration(integration, self.organisation_id)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def ensure_transcript(
        self, event_id: UUID, force_refetch: bool = False
    ) -> Meeting | None:
        """Ensure transcript exists for a calendar event.

        Tries all available integrations (via CalendarEventUserState) in
        priority order. If one integration fails with a retryable error,
        the next integration is attempted automatically.
        """
        existing = await self._get_meeting(event_id)

        # str() defensive — DB may return status as plain string, not MeetingStatus enum
        if existing and str(existing.status) == MeetingStatus.READY.value and not force_refetch:
            logger.info("Transcript exists for event %s", event_id)
            return existing

        event = await self._get_event(event_id)
        if not event or not event.online_meeting_id:
            return existing

        integrations = await self.find_integrations_for_event(event)
        if not integrations:
            logger.warning("No integrations available for event %s", event_id)
            return existing

        for integration in integrations:
            result = await self.try_fetch_with_integration(event, integration, existing)
            if result.success:
                return result.meeting
            if not result.should_retry:
                break
            logger.debug(
                "Integration %s failed for event %s, trying next...",
                integration.integration_email,
                event_id,
            )

        return existing

    async def get_transcript_status(self, event_id: UUID) -> MeetingStatus | None:
        """Quick check of transcript availability."""
        meeting = await self._get_meeting(event_id)
        return meeting.status if meeting else None

    async def get_meeting_by_event(self, event_id: UUID) -> Meeting | None:
        """Get existing Meeting record without fetching."""
        return await self._get_meeting(event_id)

    async def find_integration_by_email(
        self,
        email: str,
        *,
        integration_type: IntegrationType | None = None,
    ) -> KnowledgeIntegration | None:
        """Find active calendar integration by email."""
        if not email:
            return None
        supported_types = _compatible_calendar_integration_types(integration_type)
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                col(KnowledgeIntegration.integration_email) == email.lower(),
                col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                col(KnowledgeIntegration.integration_type).in_(supported_types),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            return (await session.execute(stmt)).scalars().first()
        return None

    async def find_integrations_for_event(self, event: CalendarEvent) -> list[KnowledgeIntegration]:
        """Find all integrations that could fetch transcript, in priority order.

        Uses CalendarEventUserState as the direct source of truth for which
        integrations have synced this event. Organiser's integration is
        prioritised first. Falls back to email lookup for old data that
        predates CalendarEventUserState.
        """
        integrations: list[KnowledgeIntegration] = []
        results: list[KnowledgeIntegration] = []
        expected_integration_type = _expected_provider_integration_type(event)
        supported_types = _compatible_calendar_integration_types(expected_integration_type)

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(KnowledgeIntegration)
                .join(
                    CalendarEventUserState,
                    col(CalendarEventUserState.integration_id)
                    == col(KnowledgeIntegration.integration_id),
                )
                .where(
                    col(CalendarEventUserState.event_id) == event.event_id,
                    col(CalendarEventUserState.is_deleted).is_(False),
                    col(KnowledgeIntegration.is_active).is_(True),
                    col(KnowledgeIntegration.connection_id).isnot(None),
                    col(KnowledgeIntegration.integration_type).in_(supported_types),
                )
            )
            results = list((await session.execute(stmt)).scalars().all())

        if not results:
            # Fallback for old data without CalendarEventUserState rows
            if not event.organiser_email:
                return []
            fallback = await self.find_integration_by_email(
                event.organiser_email,
                integration_type=expected_integration_type,
            )
            return [fallback] if fallback else []

        # Prioritise organiser's integration first
        organiser_email = (event.organiser_email or "").lower()
        organiser: list[KnowledgeIntegration] = []
        others: list[KnowledgeIntegration] = []
        seen: set[UUID] = set()
        for ki in results:
            if ki.integration_id in seen:
                continue
            seen.add(ki.integration_id)
            if (ki.integration_email or "").lower() == organiser_email:
                organiser.append(ki)
            else:
                others.append(ki)
        integrations = organiser + others

        return integrations

    async def fetch_transcript_for_event(
        self, event: CalendarEvent, max_attempts: int = 2
    ) -> TranscriptFetchStatus:
        """Main entry point for scheduled transcript fetching with retry tracking."""
        result = TranscriptFetchStatus(status="failed")
        existing = await self.get_meeting_by_event(event.event_id)
        current_attempts = existing.transcript_fetch_attempts if existing else 0
        meeting_id = existing.meeting_id if existing else None

        if getattr(self, "_enable_recall_transcripts", False):
            recall_result = await self._fetch_recall_transcript(event, existing)
            if recall_result.meeting:
                meeting_id = recall_result.meeting.meeting_id
            if recall_result.success:
                if meeting_id:
                    await self._update_attempts(meeting_id, current_attempts + 1)
                result.status = "fetched"
                result.fetched_with = MeetingRecordingProvider.RECALL_AI.value
                return result

        integrations = await self.find_integrations_for_event(event)
        if not integrations:
            logger.warning("No integrations for event %s", event.event_id)
            new_attempts = current_attempts + 1
            status = (
                MeetingStatus.NOT_AVAILABLE
                if new_attempts >= max_attempts
                else MeetingStatus.PENDING
            )
            await self._save_meeting_status(
                event.event_id, meeting_id, new_attempts, status, "No integrations available"
            )
            result.status = "not_available" if new_attempts >= max_attempts else "pending"
            return result

        last_meeting_id = meeting_id
        for integration in integrations:
            result.integrations_tried += 1
            fetch_result = await self.try_fetch_with_integration(event, integration)

            if fetch_result.meeting:
                last_meeting_id = fetch_result.meeting.meeting_id

            if fetch_result.success:
                if last_meeting_id:
                    await self._update_attempts(last_meeting_id, current_attempts + 1)
                result.status = "fetched"
                result.fetched_with = integration.integration_email
                logger.info(
                    "Transcript fetched for event %s using %s",
                    event.event_id,
                    integration.integration_email,
                )
                return result

            if not fetch_result.should_retry:
                break
            logger.debug("Integration %s failed, trying next...", integration.integration_email)

        new_attempts = current_attempts + 1
        if last_meeting_id:
            if new_attempts >= max_attempts:
                await self._save_meeting_status(
                    event.event_id,
                    last_meeting_id,
                    new_attempts,
                    MeetingStatus.NOT_AVAILABLE,
                    f"No transcript after {max_attempts} attempts with {result.integrations_tried} integration(s)",
                )
                result.status = "not_available"
            else:
                await self._save_meeting_status(
                    event.event_id,
                    last_meeting_id,
                    new_attempts,
                    MeetingStatus.PENDING,
                    f"Attempt {new_attempts}/{max_attempts}: will retry",
                )
                result.status = "pending"
        return result

    async def materialize_recall_transcript(self, event_id: UUID) -> TranscriptFetchResult:
        """Materialize the Recall transcript artifact persisted by the webhook.

        This path is intentionally Recall-only. Native Google/Teams fallback belongs
        to the legacy/manual transcript fetch path, not provider webhook handling.
        """
        event = await self._get_event(event_id)
        if not event:
            return TranscriptFetchResult(
                success=False,
                error="Calendar event not found",
                should_retry=False,
            )

        existing = await self.get_meeting_by_event(event_id)
        return await self._fetch_recall_transcript(event, existing)

    async def try_fetch_with_integration(
        self,
        event: CalendarEvent,
        integration: KnowledgeIntegration,
        existing: Meeting | None = None,
    ) -> TranscriptFetchResult:
        """Try to fetch transcript using a specific integration."""
        if not integration.connection_id:
            return TranscriptFetchResult(success=False, error="No connection_id", should_retry=True)
        if not _integration_matches_event_provider(event, integration):
            return TranscriptFetchResult(
                success=False,
                error=(
                    f"Integration type {integration.integration_type} does not match "
                    f"meeting provider {event.online_meeting_provider}"
                ),
                should_retry=True,
            )

        if event.online_meeting_provider == "teamsForBusiness":
            return await self._fetch_teams(event, integration, existing)
        elif event.online_meeting_provider == "googleMeet":
            return await self._fetch_google_meet(event, integration, existing)
        elif event.online_meeting_provider == "zoom":
            return TranscriptFetchResult(
                success=False, error="Zoom not implemented", should_retry=False
            )
        return TranscriptFetchResult(
            success=False,
            error=f"Unknown provider: {event.online_meeting_provider}",
            should_retry=False,
        )

    # -------------------------------------------------------------------------
    # Recall Meeting Bot Transcript Integration
    # -------------------------------------------------------------------------

    async def _fetch_recall_transcript(
        self,
        event: CalendarEvent,
        existing: Meeting | None,
    ) -> TranscriptFetchResult:
        """Materialize a transcript made ready by the meeting-recording webhook."""
        session = await self._get_ready_recall_session(event.event_id)
        if not session:
            return TranscriptFetchResult(success=False, error="No Recall transcript ready")

        artifact = (session.provider_payload or {}).get("transcript_artifact") or {}
        download_url = artifact.get("download_url")
        if not isinstance(download_url, str) or not download_url:
            return TranscriptFetchResult(
                success=False,
                error="Recall transcript artifact is missing download_url",
                should_retry=True,
            )

        meeting = await self._create_or_get_meeting(
            event.event_id,
            existing,
            source=MeetingSource.NATIVE_BOT,
        )

        try:
            # The download URL is already signed by Recall and came from the
            # webhook snapshot, so this provider object does not need tenant OAuth.
            provider = RecallMeetingRecordingProvider(api_key="")
            transcript_payload = await provider.fetch_transcript(download_url)
            full_transcript, segments = self._parse_recall_transcript(
                transcript_payload,
                self.organisation_id,
                meeting.meeting_id,
            )
            if not full_transcript:
                return TranscriptFetchResult(
                    success=False,
                    meeting=meeting,
                    error="Recall transcript payload was empty",
                    should_retry=True,
                )

            await self._save_transcript(
                meeting.meeting_id,
                full_transcript,
                segments,
                None,
                {
                    "source": MeetingRecordingProvider.RECALL_AI.value,
                    "meeting_recording_session_id": str(session.session_id),
                    "provider_session_id": session.provider_session_id,
                    "recording_id": artifact.get("recording_id"),
                    "transcript_id": artifact.get("transcript_id"),
                },
                existing is not None,
            )

            refreshed_meeting = await self._get_meeting(event.event_id)
            logger.info(
                "Fetched Recall transcript: %d segments, %d chars",
                len(segments),
                len(full_transcript),
                extra={
                    "event": "recall_transcript_fetched",
                    "organisation_id": str(self.organisation_id),
                    "calendar_event_id": str(event.event_id),
                    "meeting_recording_session_id": str(session.session_id),
                },
            )
            return TranscriptFetchResult(success=True, meeting=refreshed_meeting or meeting)
        except RecallProviderError as exc:
            # Returning retryable lets the webhook task retry later; Recall can lag
            # between `transcript.done` and the artifact URL becoming readable.
            logger.warning(
                "Recall transcript download failed for event %s: %s",
                event.event_id,
                exc,
            )
            return TranscriptFetchResult(
                success=False,
                meeting=meeting,
                error=str(exc)[:500],
                should_retry=True,
            )
        except Exception as exc:
            logger.warning(
                "Unexpected Recall transcript error for event %s: %s",
                event.event_id,
                exc,
            )
            return TranscriptFetchResult(
                success=False,
                meeting=meeting,
                error=str(exc)[:500],
                should_retry=True,
            )

    async def _get_ready_recall_session(self, event_id: UUID) -> MeetingRecordingSession | None:
        async for session in acquire_org_async_session(self.organisation_id):
            # The org filter is the trust boundary. Provider session ids and event ids
            # are never enough by themselves in a multi-tenant schema.
            stmt = select(MeetingRecordingSession).where(
                col(MeetingRecordingSession.organisation_id) == self.organisation_id,
                col(MeetingRecordingSession.calendar_event_id) == event_id,
                col(MeetingRecordingSession.provider) == MeetingRecordingProvider.RECALL_AI,
                col(MeetingRecordingSession.transcript_status) == MeetingTranscriptStatus.READY,
            )
            return (await session.execute(stmt)).scalars().first()
        return None

    @staticmethod
    def _parse_recall_transcript(
        payload: list[dict[str, Any]],
        organisation_id: UUID,
        meeting_id: UUID,
    ) -> tuple[str, list[TranscriptSegment]]:
        """Convert Recall's word/participant JSON into our transcript schema."""
        segments: list[TranscriptSegment] = []
        lines: list[str] = []

        for index, item in enumerate(payload):
            participant = item.get("participant") or {}
            speaker_name = participant.get("name") or item.get("speaker") or "Unknown Speaker"
            speaker_external_id = participant.get("id")
            words = item.get("words") or []
            text = item.get("text") or item.get("transcript")
            if not text and words:
                # Recall often returns word-level payloads without a segment-level
                # text field. Joining words preserves usable transcript text for LLMs.
                text = " ".join(str(word.get("text") or "").strip() for word in words).strip()
            if not isinstance(text, str) or not text.strip():
                continue

            start_time = TranscriptService._recall_word_time(
                words[0] if words else None,
                "start_timestamp",
            )
            end_time = TranscriptService._recall_word_time(
                words[-1] if words else None,
                "end_timestamp",
            )
            segments.append(
                TranscriptSegment(
                    organisation_id=organisation_id,
                    meeting_id=meeting_id,
                    segment_index=index,
                    speaker_external_id=str(speaker_external_id) if speaker_external_id else None,
                    speaker_name=str(speaker_name),
                    start_time_seconds=start_time,
                    end_time_seconds=end_time,
                    content=text.strip(),
                )
            )
            lines.append(f"{speaker_name}: {text.strip()}")

        return "\n".join(lines), segments

    @staticmethod
    def _recall_word_time(word: Any, field_name: str) -> float | None:
        """Read Recall relative word timestamps when present."""
        if not isinstance(word, dict):
            return None
        value = word.get(field_name)
        if isinstance(value, dict):
            value = value.get("relative")
        if isinstance(value, int | float):
            return float(value)
        return None

    # -------------------------------------------------------------------------
    # Teams Provider Integration
    # -------------------------------------------------------------------------

    async def _fetch_teams(
        self, event: CalendarEvent, integration: KnowledgeIntegration, existing: Meeting | None
    ) -> TranscriptFetchResult:
        """Fetch Teams transcript - acquires session only for DB operations."""
        # Step 1: Create/update meeting record (quick DB operation)
        meeting = await self._create_or_get_meeting(event.event_id, existing)

        if not _has_teams_transcript_scopes(integration):
            logger.info(
                "Skipping Teams Graph transcript fetch because integration lacks transcript scopes",
                extra={
                    "calendar_event_id": str(event.event_id),
                    "integration_id": str(integration.integration_id),
                    "missing_scopes": sorted(
                        _TEAMS_TRANSCRIPT_SCOPES.difference(set(integration.scopes or []))
                    ),
                },
            )
            return TranscriptFetchResult(
                success=False,
                meeting=meeting,
                error="Teams transcript permission not granted",
                should_retry=False,
            )

        try:
            # Step 2: External API calls (NO session held here)
            connection_id: str = integration.connection_id  # type: ignore[assignment]
            self._teams.with_token_info(self._build_token_info(integration))
            actual_meeting_id = await self._teams.resolve_meeting_id(
                connection_id,
                event.conference_link,
            )
            if not actual_meeting_id:
                logger.info("Could not resolve Teams meeting ID for event %s", event.event_id)
                return TranscriptFetchResult(
                    success=False,
                    meeting=meeting,
                    error="Could not resolve meeting ID",
                    should_retry=True,
                )

            transcript_data = await self._teams.fetch_transcript(connection_id, actual_meeting_id)
            if not transcript_data:
                return TranscriptFetchResult(
                    success=False, meeting=meeting, error="No transcript found", should_retry=True
                )

            # Step 3: Parse (no DB, no network)
            full_transcript, segments = self._teams.parse_vtt(
                transcript_data["vtt_content"], meeting.meeting_id
            )

            # Step 4: Save results (quick DB operation)
            await self._save_transcript(
                meeting.meeting_id,
                full_transcript,
                segments,
                transcript_data.get("language"),
                {
                    **(transcript_data.get("metadata") or {}),
                    "fetched_with_integration": str(integration.integration_id),
                    "fetched_with_email": integration.integration_email,
                },
                existing is not None,
            )

            # Refresh meeting object
            refreshed_meeting = await self._get_meeting(event.event_id)
            logger.info(
                "Fetched Teams transcript: %d segments, %d chars",
                len(segments),
                len(full_transcript),
            )
            return TranscriptFetchResult(success=True, meeting=refreshed_meeting or meeting)

        except TranscriptAuthError as e:
            logger.warning("Connection error with %s: %s", integration.integration_email, e)
            return TranscriptFetchResult(
                success=False, meeting=meeting, error=str(e)[:500], should_retry=True
            )
        except Exception as e:
            logger.warning("Unexpected error with %s: %s", integration.integration_email, e)
            return TranscriptFetchResult(
                success=False, meeting=meeting, error=str(e)[:500], should_retry=False
            )

    # -------------------------------------------------------------------------
    # Google Meet Provider Integration
    # -------------------------------------------------------------------------

    async def _fetch_google_meet(
        self, event: CalendarEvent, integration: KnowledgeIntegration, existing: Meeting | None
    ) -> TranscriptFetchResult:
        """Fetch Google Meet transcript — mirrors _fetch_teams structure."""
        meeting = await self._create_or_get_meeting(
            event.event_id, existing, source=MeetingSource.GOOGLE_MEET
        )

        try:
            # Step 1: Resolve conference record (no session held)
            meeting_code = event.online_meeting_id
            conn_id: str = integration.connection_id  # type: ignore[assignment]
            self._google_meet.with_token_info(self._build_token_info(integration))
            if not meeting_code:
                return TranscriptFetchResult(
                    success=False,
                    meeting=meeting,
                    error="No meeting code",
                    should_retry=False,
                )

            conference_record = await self._google_meet.resolve_conference_record(
                conn_id,
                meeting_code,
            )
            if not conference_record:
                return TranscriptFetchResult(
                    success=False,
                    meeting=meeting,
                    error="No conference record found",
                    should_retry=True,
                )

            # Step 2: Fetch transcript data (no session held)
            transcript_data = await self._google_meet.fetch_transcript(
                conn_id,
                conference_record,
            )
            if not transcript_data:
                return TranscriptFetchResult(
                    success=False,
                    meeting=meeting,
                    error="No transcript found",
                    should_retry=True,
                )

            # Step 3: Parse (pure computation)
            full_transcript, segments = self._google_meet.parse_entries(
                transcript_data["entries"],
                transcript_data["participant_map"],
                meeting.meeting_id,
            )

            # Step 4: Save (quick DB operation)
            await self._save_transcript(
                meeting.meeting_id,
                full_transcript,
                segments,
                transcript_data.get("language"),
                {
                    "fetched_with_integration": str(integration.integration_id),
                    "fetched_with_email": integration.integration_email,
                    "conference_record": conference_record,
                },
                existing is not None,
            )

            refreshed_meeting = await self._get_meeting(event.event_id)
            logger.info(
                "Fetched Google Meet transcript: %d segments, %d chars",
                len(segments),
                len(full_transcript),
            )
            return TranscriptFetchResult(success=True, meeting=refreshed_meeting or meeting)

        except TranscriptAuthError as e:
            logger.warning("Connection error with %s: %s", integration.integration_email, e)
            return TranscriptFetchResult(
                success=False,
                meeting=meeting,
                error=str(e)[:500],
                should_retry=True,
            )
        except Exception as e:
            logger.warning("Unexpected error with %s: %s", integration.integration_email, e)
            return TranscriptFetchResult(
                success=False,
                meeting=meeting,
                error=str(e)[:500],
                should_retry=False,
            )

    # -------------------------------------------------------------------------
    # Database Operations (each acquires and releases session immediately)
    # -------------------------------------------------------------------------

    async def _get_meeting(self, event_id: UUID) -> Meeting | None:
        async for session in acquire_org_async_session(self.organisation_id):
            # Meeting rows are scoped by org plus event. The event id may come from
            # webhook payloads or API requests, so keep organisation_id in every lookup.
            stmt = select(Meeting).where(
                col(Meeting.organisation_id) == self.organisation_id,
                col(Meeting.calendar_event_id) == event_id,
            )
            return (await session.execute(stmt)).scalars().first()
        return None

    async def _get_event(self, event_id: UUID) -> CalendarEvent | None:
        async for session in acquire_org_async_session(self.organisation_id):
            # Same tenant boundary as meeting lookup: event_id alone is not trusted.
            stmt = select(CalendarEvent).where(
                col(CalendarEvent.event_id) == event_id,
                col(CalendarEvent.organisation_id) == self.organisation_id,
            )
            return (await session.execute(stmt)).scalars().first()
        return None

    async def _get_integration(self, integration_id: UUID | None) -> KnowledgeIntegration | None:
        if not integration_id:
            return None
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            )
            return (await session.execute(stmt)).scalars().first()
        return None

    async def _create_or_get_meeting(
        self,
        event_id: UUID,
        existing: Meeting | None,
        source: MeetingSource = MeetingSource.TEAMS_TRANSCRIPT,
    ) -> Meeting:
        async for session in acquire_org_async_session(self.organisation_id):
            if existing:
                stmt = select(Meeting).where(col(Meeting.meeting_id) == existing.meeting_id)
                meeting = (await session.execute(stmt)).scalars().first()
                if meeting:
                    meeting.status = MeetingStatus.FETCHING
                    await session.commit()
                    return meeting

            # Re-read by event because webhook retries can race with a prior task
            # that created the Meeting after our caller loaded `existing`.
            stmt = select(Meeting).where(col(Meeting.calendar_event_id) == event_id)
            meeting = (await session.execute(stmt)).scalars().first()
            if meeting:
                meeting.status = MeetingStatus.FETCHING
                await session.commit()
                return meeting

            # First writer creates the transcript container; later retries take the
            # branch above and update the same row.
            meeting = Meeting(
                organisation_id=self.organisation_id,
                calendar_event_id=event_id,
                source=source,
                status=MeetingStatus.FETCHING,
            )
            session.add(meeting)
            await session.commit()
            await session.refresh(meeting)
            return meeting
        raise RuntimeError("Failed to acquire session")

    async def _save_transcript(
        self,
        meeting_id: UUID,
        full_transcript: str,
        segments: list[TranscriptSegment],
        language: str | None,
        source_data: dict,
        delete_old_segments: bool,
    ) -> None:
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(Meeting).where(col(Meeting.meeting_id) == meeting_id)
            meeting = (await session.execute(stmt)).scalars().first()
            if not meeting:
                return

            adapter = MeetingEventAdapter(self.organisation_id)
            previous_source_event_id = adapter.transcript_source_event_id(meeting)
            previous_fetched_at = meeting.transcript_fetched_at
            meeting.status = MeetingStatus.READY
            meeting.full_transcript = full_transcript
            meeting.transcript_language = language
            meeting.source_data = source_data
            meeting.transcript_fetched_at = (
                previous_fetched_at
                if previous_fetched_at
                and previous_source_event_id == adapter.transcript_source_event_id(meeting)
                else datetime.now(UTC)
            )

            if delete_old_segments:
                old_stmt = select(TranscriptSegment).where(
                    col(TranscriptSegment.meeting_id) == meeting_id
                )
                for seg in (await session.execute(old_stmt)).scalars().all():
                    await session.delete(seg)

            for segment in segments:
                session.add(segment)

            context = await load_meeting_event_context(
                session,
                self.organisation_id,
                meeting,
            )
            envelope = adapter.transcript_ready(
                meeting,
                context=context,
            )
            await AutomationEventDispatchService(
                session,
                self.organisation_id,
                registry=AUTOMATION_REGISTRY,
            ).enqueue(envelope)
            await session.commit()
            return

    async def _save_meeting_status(
        self,
        event_id: UUID,
        meeting_id: UUID | None,
        attempts: int,
        status: MeetingStatus,
        message: str,
    ) -> None:
        async for session in acquire_org_async_session(self.organisation_id):
            if meeting_id:
                stmt = select(Meeting).where(col(Meeting.meeting_id) == meeting_id)
                meeting = (await session.execute(stmt)).scalars().first()
                if meeting:
                    meeting.transcript_fetch_attempts = attempts
                    meeting.status = status
                    meeting.status_message = message
            else:
                meeting = Meeting(
                    organisation_id=self.organisation_id,
                    calendar_event_id=event_id,
                    status=status,
                    status_message=message,
                    transcript_fetch_attempts=attempts,
                )
                session.add(meeting)
            await session.commit()
            return

    async def _update_attempts(self, meeting_id: UUID, attempts: int) -> None:
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(Meeting).where(col(Meeting.meeting_id) == meeting_id)
            meeting = (await session.execute(stmt)).scalars().first()
            if meeting:
                meeting.transcript_fetch_attempts = attempts
                await session.commit()
            return
