"""Comprehensive tests for TranscriptService.

Covers all meaningful public methods: ensure_transcript, find_integrations_for_event,
try_fetch_with_integration, _fetch_teams, fetch_transcript_for_event, and
find_integration_by_email.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ClauseElement
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.domain.models.organisation_tables.sync.meeting_intelligence import MeetingStatus
from src.domain.models.organisation_tables.sync.meeting_recording import MeetingRecordingProvider
from src.service.integrations.transcripts.google_meet_provider import TranscriptAuthError
from src.service.integrations.transcripts.service import (
    TranscriptFetchResult,
    TranscriptService,
)

# =============================================================================
# Helpers
# =============================================================================


@dataclass
class FakeIntegration:
    """Minimal KnowledgeIntegration stand-in."""

    integration_id: UUID = field(default_factory=uuid4)
    integration_email: str = "user@example.com"
    connection_id: str | None = "conn-1"
    is_active: bool = True
    integration_type: IntegrationType = IntegrationType.OUTLOOK_CALENDAR
    organisation_id: UUID = field(default_factory=uuid4)
    credentials: dict[str, str] = field(
        default_factory=lambda: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }
    )
    access_token_expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)


@dataclass
class FakeCalendarEvent:
    """Minimal CalendarEvent stand-in."""

    event_id: UUID = field(default_factory=uuid4)
    organisation_id: UUID = field(default_factory=uuid4)
    organiser_email: str = "organiser@example.com"
    online_meeting_id: str | None = "meeting-123"
    online_meeting_provider: str = "teamsForBusiness"
    conference_link: str = "https://teams.link"
    integration_id: UUID | None = None
    start_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    end_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(hours=1))


@dataclass
class FakeMeeting:
    """Minimal Meeting stand-in."""

    meeting_id: UUID = field(default_factory=uuid4)
    calendar_event_id: UUID = field(default_factory=uuid4)
    status: str = "pending"
    transcript_fetch_attempts: int = 0


def _make_service(org_id: UUID | None = None) -> TranscriptService:
    """Create a TranscriptService with patched providers."""
    svc = TranscriptService.__new__(TranscriptService)
    svc.organisation_id = org_id or uuid4()
    svc._teams = MagicMock()
    svc._google_meet = MagicMock()
    svc._enable_recall_transcripts = False
    return svc


def _fake_integration_with_teams_transcript_scopes() -> FakeIntegration:
    return FakeIntegration(
        scopes=[
            "offline_access",
            "User.Read",
            "Calendars.Read",
            "OnlineMeetings.Read",
            "OnlineMeetingTranscript.Read.All",
        ]
    )


def _fake_session_gen(results):
    """Create a fake async generator that yields a mock session.

    `results` is a list — each session.execute() pops the next one.
    """
    mock_scalars = MagicMock()
    if isinstance(results, list):
        mock_scalars.all.return_value = results
        mock_scalars.first.return_value = results[0] if results else None
    else:
        mock_scalars.all.return_value = [results] if results else []
        mock_scalars.first.return_value = results

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def gen(oid):
        yield mock_session

    return gen


class _CapturingSession:
    def __init__(self, results):
        self.statements: list[ClauseElement] = []
        self._session_gen = _fake_session_gen(results)
        self._session = None

    async def execute(self, stmt: ClauseElement):
        self.statements.append(stmt)
        if self._session is None:
            async for session in self._session_gen(None):
                self._session = session
                break
        return await self._session.execute(stmt)


def _capturing_session_gen(results) -> tuple[_CapturingSession, object]:
    session = _CapturingSession(results)

    async def gen(_oid):
        yield session

    return session, gen


def _compile_sql(stmt: ClauseElement) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# =============================================================================
# TestEnsureTranscript
# =============================================================================


class TestEnsureTranscript:
    """Tests for TranscriptService.ensure_transcript."""

    @pytest.mark.asyncio
    async def test_returns_early_for_ready_status(self) -> None:
        """READY meeting returns without fetching."""
        svc = _make_service()
        ready_meeting = FakeMeeting(status="ready")

        svc._get_meeting = AsyncMock(return_value=ready_meeting)  # type: ignore[method-assign]
        svc._get_event = AsyncMock()  # type: ignore[method-assign]

        result = await svc.ensure_transcript(ready_meeting.calendar_event_id)

        assert result == ready_meeting
        svc._get_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_refetch_overrides_ready(self) -> None:
        """READY + force_refetch=True proceeds to fetch."""
        svc = _make_service()
        event = FakeCalendarEvent()
        ready_meeting = FakeMeeting(status="ready", calendar_event_id=event.event_id)
        new_meeting = FakeMeeting(status="ready")
        int1 = FakeIntegration()

        svc._get_meeting = AsyncMock(return_value=ready_meeting)  # type: ignore[method-assign]
        svc._get_event = AsyncMock(return_value=event)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[int1])  # type: ignore[method-assign]
        svc.try_fetch_with_integration = AsyncMock(  # type: ignore[method-assign]
            return_value=TranscriptFetchResult(success=True, meeting=new_meeting),
        )

        result = await svc.ensure_transcript(event.event_id, force_refetch=True)

        assert result == new_meeting
        svc._get_event.assert_awaited_once()
        svc.try_fetch_with_integration.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_event_returns_existing(self) -> None:
        """Event not found returns existing meeting."""
        svc = _make_service()
        existing = FakeMeeting(status="pending")

        svc._get_meeting = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc._get_event = AsyncMock(return_value=None)  # type: ignore[method-assign]

        result = await svc.ensure_transcript(existing.calendar_event_id)

        assert result == existing

    @pytest.mark.asyncio
    async def test_no_online_meeting_id_returns_existing(self) -> None:
        """Event without online_meeting_id returns existing meeting."""
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_id=None)
        existing = FakeMeeting(status="pending")

        svc._get_meeting = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc._get_event = AsyncMock(return_value=event)  # type: ignore[method-assign]

        result = await svc.ensure_transcript(existing.calendar_event_id)

        assert result == existing

    @pytest.mark.asyncio
    async def test_no_integrations_returns_existing(self) -> None:
        """No integrations available returns existing meeting."""
        svc = _make_service()
        event = FakeCalendarEvent()
        existing = FakeMeeting(status="pending")

        svc._get_meeting = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc._get_event = AsyncMock(return_value=event)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[])  # type: ignore[method-assign]

        result = await svc.ensure_transcript(existing.calendar_event_id)

        assert result == existing

    @pytest.mark.asyncio
    async def test_tries_multiple_integrations(self) -> None:
        """First integration fails (retryable) then second succeeds."""
        svc = _make_service()
        event = FakeCalendarEvent()
        int1 = FakeIntegration(integration_email="first@example.com")
        int2 = FakeIntegration(integration_email="second@example.com")
        success_meeting = FakeMeeting(status="ready")

        svc._get_meeting = AsyncMock(return_value=None)  # type: ignore[method-assign]
        svc._get_event = AsyncMock(return_value=event)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[int1, int2])  # type: ignore[method-assign]
        svc.try_fetch_with_integration = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                TranscriptFetchResult(success=False, error="timeout", should_retry=True),
                TranscriptFetchResult(success=True, meeting=success_meeting),
            ]
        )

        result = await svc.ensure_transcript(event.event_id)

        assert result == success_meeting
        assert svc.try_fetch_with_integration.await_count == 2

    @pytest.mark.asyncio
    async def test_stops_on_non_retryable_failure(self) -> None:
        """Non-retryable failure does NOT try second integration."""
        svc = _make_service()
        event = FakeCalendarEvent()
        int1 = FakeIntegration(integration_email="first@example.com")
        int2 = FakeIntegration(integration_email="second@example.com")

        svc._get_meeting = AsyncMock(return_value=None)  # type: ignore[method-assign]
        svc._get_event = AsyncMock(return_value=event)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[int1, int2])  # type: ignore[method-assign]
        svc.try_fetch_with_integration = AsyncMock(  # type: ignore[method-assign]
            return_value=TranscriptFetchResult(
                success=False, error="Zoom not implemented", should_retry=False
            ),
        )

        result = await svc.ensure_transcript(event.event_id)

        assert result is None
        assert svc.try_fetch_with_integration.await_count == 1


# =============================================================================
# TestFindIntegrationsForEvent
# =============================================================================


class TestFindIntegrationsForEvent:
    """Tests for TranscriptService.find_integrations_for_event."""

    @pytest.mark.asyncio
    async def test_returns_integrations_from_user_states(self) -> None:
        """CalendarEventUserState rows return matching integrations."""
        org_id = uuid4()
        svc = _make_service(org_id)
        event = FakeCalendarEvent(organisation_id=org_id)

        int1 = FakeIntegration(integration_email="alice@example.com", organisation_id=org_id)
        int2 = FakeIntegration(integration_email="bob@example.com", organisation_id=org_id)
        capture, session_gen = _capturing_session_gen([int1, int2])

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=session_gen,
        ):
            result = await svc.find_integrations_for_event(event)  # type: ignore[arg-type]

        assert len(result) == 2
        emails = {r.integration_email for r in result}
        assert emails == {"alice@example.com", "bob@example.com"}
        stmt = _compile_sql(capture.statements[0])
        assert "'microsoft'" in stmt
        assert "'google'" not in stmt
        assert "'google_calendar'" not in stmt
        assert "'outlook_calendar'" not in stmt

    @pytest.mark.asyncio
    async def test_google_meet_lookup_uses_google_provider_parent_only(self) -> None:
        """Google Meet transcript lookup must not return Microsoft credentials."""
        org_id = uuid4()
        svc = _make_service(org_id)
        event = FakeCalendarEvent(organisation_id=org_id, online_meeting_provider="googleMeet")
        capture, session_gen = _capturing_session_gen([])

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=session_gen,
        ):
            result = await svc.find_integrations_for_event(event)  # type: ignore[arg-type]

        assert result == []
        stmt = _compile_sql(capture.statements[0])
        assert "'google'" in stmt
        assert "'microsoft'" not in stmt

    @pytest.mark.asyncio
    async def test_prioritises_organiser_integration(self) -> None:
        """Organiser's integration appears first."""
        org_id = uuid4()
        svc = _make_service(org_id)
        event = FakeCalendarEvent(organisation_id=org_id, organiser_email="organiser@example.com")

        other_int = FakeIntegration(integration_email="other@example.com", organisation_id=org_id)
        org_int = FakeIntegration(integration_email="organiser@example.com", organisation_id=org_id)

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=_fake_session_gen([other_int, org_int]),
        ):
            result = await svc.find_integrations_for_event(event)  # type: ignore[arg-type]

        assert len(result) == 2
        assert result[0].integration_email == "organiser@example.com"
        assert result[1].integration_email == "other@example.com"

    @pytest.mark.asyncio
    async def test_deduplicates_by_integration_id(self) -> None:
        """Same integration_id appearing twice is returned once."""
        org_id = uuid4()
        svc = _make_service(org_id)
        event = FakeCalendarEvent(organisation_id=org_id)

        shared_id = uuid4()
        dup1 = FakeIntegration(
            integration_id=shared_id, integration_email="same@example.com", organisation_id=org_id
        )
        dup2 = FakeIntegration(
            integration_id=shared_id, integration_email="same@example.com", organisation_id=org_id
        )

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=_fake_session_gen([dup1, dup2]),
        ):
            result = await svc.find_integrations_for_event(event)  # type: ignore[arg-type]

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_case_insensitive_organiser_matching(self) -> None:
        """Mixed-case organiser email still matched for prioritisation."""
        org_id = uuid4()
        svc = _make_service(org_id)
        event = FakeCalendarEvent(organisation_id=org_id, organiser_email="Organiser@Example.COM")

        int1 = FakeIntegration(integration_email="organiser@example.com", organisation_id=org_id)

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=_fake_session_gen([int1]),
        ):
            result = await svc.find_integrations_for_event(event)  # type: ignore[arg-type]

        assert len(result) == 1
        assert result[0].integration_email == "organiser@example.com"

    @pytest.mark.asyncio
    async def test_fallback_to_organiser_email(self) -> None:
        """No UserState rows falls back to email lookup."""
        org_id = uuid4()
        svc = _make_service(org_id)
        event = FakeCalendarEvent(organisation_id=org_id, organiser_email="organiser@example.com")
        fallback_int = FakeIntegration(
            integration_email="organiser@example.com", organisation_id=org_id
        )

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=_fake_session_gen([]),
        ):
            svc.find_integration_by_email = AsyncMock(return_value=fallback_int)  # type: ignore[method-assign]
            result = await svc.find_integrations_for_event(event)  # type: ignore[arg-type]

        assert len(result) == 1
        assert result[0].integration_email == "organiser@example.com"
        svc.find_integration_by_email.assert_awaited_once_with(
            "organiser@example.com",
            integration_type=IntegrationType.MICROSOFT,
        )

    @pytest.mark.asyncio
    async def test_fallback_returns_empty_when_no_email_match(self) -> None:
        """No UserState rows + email lookup fails returns empty list."""
        org_id = uuid4()
        svc = _make_service(org_id)
        event = FakeCalendarEvent(organisation_id=org_id, organiser_email="nobody@example.com")

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=_fake_session_gen([]),
        ):
            svc.find_integration_by_email = AsyncMock(return_value=None)  # type: ignore[method-assign]
            result = await svc.find_integrations_for_event(event)  # type: ignore[arg-type]

        assert result == []


# =============================================================================
# TestTryFetchWithIntegration
# =============================================================================


class TestTryFetchWithIntegration:
    """Tests for TranscriptService.try_fetch_with_integration."""

    @pytest.mark.asyncio
    async def test_no_connection_id_returns_retryable(self) -> None:
        """connection_id=None gives retryable failure."""
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = FakeIntegration(connection_id=None)

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True
        assert "connection_id" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_teams_provider_delegates_to_fetch_teams(self) -> None:
        """teamsForBusiness provider delegates to _fetch_teams."""
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_provider="teamsForBusiness")
        integration = FakeIntegration(integration_type=IntegrationType.MICROSOFT)
        expected = TranscriptFetchResult(success=True, meeting=FakeMeeting())

        svc._fetch_teams = AsyncMock(return_value=expected)  # type: ignore[method-assign]

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result == expected
        svc._fetch_teams.assert_awaited_once_with(event, integration, None)

    @pytest.mark.asyncio
    async def test_teams_provider_rejects_google_parent_integration(self) -> None:
        """Teams transcript fetch must not use Google credentials."""
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_provider="teamsForBusiness")
        integration = FakeIntegration(integration_type=IntegrationType.GOOGLE)

        svc._fetch_teams = AsyncMock()  # type: ignore[method-assign]

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True
        assert "does not match" in (result.error or "")
        svc._fetch_teams.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zoom_returns_non_retryable(self) -> None:
        """zoom provider returns non-retryable failure."""
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_provider="zoom")
        integration = FakeIntegration()

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is False
        assert "zoom" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_google_meet_delegates_to_fetch_google_meet(self) -> None:
        """googleMeet provider delegates to _fetch_google_meet."""
        svc = _make_service()
        svc._google_meet = MagicMock()  # provider needed on service
        event = FakeCalendarEvent(online_meeting_provider="googleMeet")
        integration = FakeIntegration(integration_type=IntegrationType.GOOGLE)
        expected = TranscriptFetchResult(success=True, meeting=FakeMeeting())

        svc._fetch_google_meet = AsyncMock(return_value=expected)  # type: ignore[method-assign]

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result == expected
        svc._fetch_google_meet.assert_awaited_once_with(event, integration, None)

    @pytest.mark.asyncio
    async def test_google_provider_parent_delegates_to_fetch_google_meet(self) -> None:
        """Provider-parent Google integrations are valid for Google Meet transcript fetch."""
        svc = _make_service()
        svc._google_meet = MagicMock()
        event = FakeCalendarEvent(online_meeting_provider="googleMeet")
        integration = FakeIntegration(integration_type=IntegrationType.GOOGLE)
        expected = TranscriptFetchResult(success=True, meeting=FakeMeeting())

        svc._fetch_google_meet = AsyncMock(return_value=expected)  # type: ignore[method-assign]

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result == expected
        svc._fetch_google_meet.assert_awaited_once_with(event, integration, None)

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_non_retryable(self) -> None:
        """Unknown provider string returns non-retryable failure."""
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_provider="webex")
        integration = FakeIntegration()

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is False
        assert "unknown" in (result.error or "").lower()


# =============================================================================
# TestFetchTeams
# =============================================================================


class TestFetchTeams:
    """Tests for TranscriptService._fetch_teams."""

    @pytest.mark.asyncio
    async def test_skips_graph_transcript_fetch_without_transcript_scope(self) -> None:
        """Teams Graph transcript fetch is optional and must not block calendar connect."""
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = FakeIntegration(
            scopes=["offline_access", "User.Read", "Calendars.Read", "OnlineMeetings.Read"]
        )
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._teams.resolve_meeting_id = AsyncMock()
        svc._teams.fetch_transcript = AsyncMock()

        result = await svc._fetch_teams(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is False
        assert result.error == "Teams transcript permission not granted"
        assert result.meeting == meeting
        svc._teams.resolve_meeting_id.assert_not_awaited()
        svc._teams.fetch_transcript.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        """resolve -> fetch -> parse -> save -> returns success with meeting."""
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = _fake_integration_with_teams_transcript_scopes()
        meeting = FakeMeeting()
        refreshed_meeting = FakeMeeting(status="ready")

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._teams.resolve_meeting_id = AsyncMock(return_value="teams-id-123")
        svc._teams.fetch_transcript = AsyncMock(
            return_value={
                "vtt_content": "WEBVTT\n\n00:00.000 --> 00:01.000\nHello",
                "language": "en",
                "metadata": {"source": "teams"},
            }
        )
        svc._teams.parse_vtt = MagicMock(return_value=("Hello", []))
        svc._save_transcript = AsyncMock()  # type: ignore[method-assign]
        svc._get_meeting = AsyncMock(return_value=refreshed_meeting)  # type: ignore[method-assign]

        result = await svc._fetch_teams(event, integration, None)  # type: ignore[arg-type]

        assert result.success is True
        assert result.meeting == refreshed_meeting
        svc._create_or_get_meeting.assert_awaited_once()
        svc._teams.resolve_meeting_id.assert_awaited_once()
        svc._teams.fetch_transcript.assert_awaited_once()
        svc._teams.parse_vtt.assert_called_once()
        svc._save_transcript.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolve_meeting_id_fails(self) -> None:
        """resolve_meeting_id returns None gives retryable failure."""
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = _fake_integration_with_teams_transcript_scopes()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._teams.resolve_meeting_id = AsyncMock(return_value=None)
        svc._teams.fetch_transcript = AsyncMock()

        result = await svc._fetch_teams(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True
        assert result.meeting == meeting
        svc._teams.fetch_transcript.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_transcript_returns_none(self) -> None:
        """fetch_transcript returns None gives retryable failure."""
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = _fake_integration_with_teams_transcript_scopes()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._teams.resolve_meeting_id = AsyncMock(return_value="teams-id-123")
        svc._teams.fetch_transcript = AsyncMock(return_value=None)

        result = await svc._fetch_teams(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True
        assert result.meeting == meeting

    @pytest.mark.asyncio
    async def test_transcript_auth_error_is_retryable(self) -> None:
        """TranscriptAuthError is caught and returns retryable failure."""
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = _fake_integration_with_teams_transcript_scopes()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._teams.resolve_meeting_id = AsyncMock(side_effect=TranscriptAuthError("token expired"))

        result = await svc._fetch_teams(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True
        assert "token expired" in (result.error or "")

    @pytest.mark.asyncio
    async def test_unexpected_error_is_non_retryable(self) -> None:
        """Generic Exception is caught and returns non-retryable failure."""
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = _fake_integration_with_teams_transcript_scopes()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._teams.resolve_meeting_id = AsyncMock(side_effect=RuntimeError("unexpected"))

        result = await svc._fetch_teams(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is False
        assert "unexpected" in (result.error or "")


# =============================================================================
# TestFetchTranscriptForEvent
# =============================================================================


class TestFetchTranscriptForEvent:
    """Tests for TranscriptService.fetch_transcript_for_event."""

    @pytest.mark.asyncio
    async def test_ready_recall_artifact_is_preferred_over_calendar_integrations(self) -> None:
        """Recall webhook readiness materializes before Teams/Google fallback."""
        svc = _make_service()
        svc._enable_recall_transcripts = True
        event = FakeCalendarEvent()
        existing = FakeMeeting(transcript_fetch_attempts=0)
        recall_meeting = FakeMeeting(status="ready")

        svc.get_meeting_by_event = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc._fetch_recall_transcript = AsyncMock(  # type: ignore[method-assign]
            return_value=TranscriptFetchResult(success=True, meeting=recall_meeting),
        )
        svc._update_attempts = AsyncMock()  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock()  # type: ignore[method-assign]

        result = await svc.fetch_transcript_for_event(event, max_attempts=2)  # type: ignore[arg-type]

        assert result.status == "fetched"
        assert result.fetched_with == MeetingRecordingProvider.RECALL_AI.value
        svc._update_attempts.assert_awaited_once_with(recall_meeting.meeting_id, 1)
        svc.find_integrations_for_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_recall_fetch_preserves_created_meeting_for_status_update(self) -> None:
        """Recall-only failures update the created Meeting instead of inserting again."""
        svc = _make_service()
        svc._enable_recall_transcripts = True
        event = FakeCalendarEvent()
        created_meeting = FakeMeeting(transcript_fetch_attempts=0)

        svc.get_meeting_by_event = AsyncMock(return_value=None)  # type: ignore[method-assign]
        svc._fetch_recall_transcript = AsyncMock(  # type: ignore[method-assign]
            return_value=TranscriptFetchResult(
                success=False,
                meeting=created_meeting,
                error="download failed",
                should_retry=True,
            ),
        )
        svc.find_integrations_for_event = AsyncMock(return_value=[])  # type: ignore[method-assign]
        svc._save_meeting_status = AsyncMock()  # type: ignore[method-assign]

        result = await svc.fetch_transcript_for_event(event, max_attempts=2)  # type: ignore[arg-type]

        assert result.status == "pending"
        svc._save_meeting_status.assert_awaited_once()
        call_args = svc._save_meeting_status.call_args
        assert call_args[0][1] == created_meeting.meeting_id
        assert call_args[0][3] == MeetingStatus.PENDING

    @pytest.mark.asyncio
    async def test_no_integrations_pending_status(self) -> None:
        """No integrations, attempt 1/2 sets PENDING status."""
        svc = _make_service()
        event = FakeCalendarEvent()
        existing = FakeMeeting(transcript_fetch_attempts=0)

        svc.get_meeting_by_event = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[])  # type: ignore[method-assign]
        svc._save_meeting_status = AsyncMock()  # type: ignore[method-assign]

        result = await svc.fetch_transcript_for_event(event, max_attempts=2)  # type: ignore[arg-type]

        assert result.status == "pending"
        svc._save_meeting_status.assert_awaited_once()
        call_args = svc._save_meeting_status.call_args
        assert call_args[0][3] == MeetingStatus.PENDING

    @pytest.mark.asyncio
    async def test_no_integrations_not_available_at_max(self) -> None:
        """No integrations, attempt 2/2 sets NOT_AVAILABLE status."""
        svc = _make_service()
        event = FakeCalendarEvent()
        # Already tried once before
        existing = FakeMeeting(transcript_fetch_attempts=1)

        svc.get_meeting_by_event = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[])  # type: ignore[method-assign]
        svc._save_meeting_status = AsyncMock()  # type: ignore[method-assign]

        result = await svc.fetch_transcript_for_event(event, max_attempts=2)  # type: ignore[arg-type]

        assert result.status == "not_available"
        call_args = svc._save_meeting_status.call_args
        assert call_args[0][3] == MeetingStatus.NOT_AVAILABLE


class TestRecallTranscriptParsing:
    """Tests for Recall transcript artifact parsing."""

    def test_parse_recall_transcript_words_into_segments(self) -> None:
        meeting_id = uuid4()
        payload = [
            {
                "participant": {"id": 100, "name": "Alice"},
                "words": [
                    {"text": "hello", "start_timestamp": {"relative": 1.0}},
                    {"text": "there", "end_timestamp": {"relative": 2.2}},
                ],
            },
            {
                "participant": {"id": 200, "name": "Bob"},
                "text": "hi Alice",
            },
        ]

        full_text, segments = TranscriptService._parse_recall_transcript(payload, meeting_id)

        assert full_text == "Alice: hello there\nBob: hi Alice"
        assert len(segments) == 2
        assert segments[0].speaker_name == "Alice"
        assert segments[0].speaker_external_id == "100"
        assert segments[0].start_time_seconds == 1.0
        assert segments[0].end_time_seconds == 2.2
        assert segments[1].content == "hi Alice"

    @pytest.mark.asyncio
    async def test_success_returns_fetched(self) -> None:
        """Integration succeeds returns 'fetched' and updates attempts."""
        svc = _make_service()
        event = FakeCalendarEvent()
        existing = FakeMeeting(transcript_fetch_attempts=0)
        int1 = FakeIntegration(integration_email="fetcher@example.com")
        success_meeting = FakeMeeting(status="ready")

        svc.get_meeting_by_event = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[int1])  # type: ignore[method-assign]
        svc.try_fetch_with_integration = AsyncMock(  # type: ignore[method-assign]
            return_value=TranscriptFetchResult(success=True, meeting=success_meeting),
        )
        svc._update_attempts = AsyncMock()  # type: ignore[method-assign]

        result = await svc.fetch_transcript_for_event(event, max_attempts=2)  # type: ignore[arg-type]

        assert result.status == "fetched"
        assert result.fetched_with == "fetcher@example.com"
        svc._update_attempts.assert_awaited_once_with(success_meeting.meeting_id, 1)

    @pytest.mark.asyncio
    async def test_all_fail_before_max_attempts_pending(self) -> None:
        """All integrations fail, attempt 1/2 sets PENDING."""
        svc = _make_service()
        event = FakeCalendarEvent()
        existing = FakeMeeting(transcript_fetch_attempts=0)
        int1 = FakeIntegration()

        svc.get_meeting_by_event = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[int1])  # type: ignore[method-assign]
        # Fails but non-retryable (breaks out of loop)
        fail_meeting = FakeMeeting()
        svc.try_fetch_with_integration = AsyncMock(  # type: ignore[method-assign]
            return_value=TranscriptFetchResult(
                success=False, meeting=fail_meeting, error="fail", should_retry=False
            ),
        )
        svc._save_meeting_status = AsyncMock()  # type: ignore[method-assign]

        result = await svc.fetch_transcript_for_event(event, max_attempts=2)  # type: ignore[arg-type]

        assert result.status == "pending"
        call_args = svc._save_meeting_status.call_args
        assert call_args[0][3] == MeetingStatus.PENDING

    @pytest.mark.asyncio
    async def test_all_fail_at_max_attempts_not_available(self) -> None:
        """All integrations fail, attempt 2/2 sets NOT_AVAILABLE."""
        svc = _make_service()
        event = FakeCalendarEvent()
        existing = FakeMeeting(transcript_fetch_attempts=1)
        int1 = FakeIntegration()

        svc.get_meeting_by_event = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        svc.find_integrations_for_event = AsyncMock(return_value=[int1])  # type: ignore[method-assign]
        fail_meeting = FakeMeeting()
        svc.try_fetch_with_integration = AsyncMock(  # type: ignore[method-assign]
            return_value=TranscriptFetchResult(
                success=False, meeting=fail_meeting, error="fail", should_retry=False
            ),
        )
        svc._save_meeting_status = AsyncMock()  # type: ignore[method-assign]

        result = await svc.fetch_transcript_for_event(event, max_attempts=2)  # type: ignore[arg-type]

        assert result.status == "not_available"
        call_args = svc._save_meeting_status.call_args
        assert call_args[0][3] == MeetingStatus.NOT_AVAILABLE


# =============================================================================
# TestFindIntegrationByEmail
# =============================================================================


class TestFindIntegrationByEmail:
    """Tests for TranscriptService.find_integration_by_email."""

    @pytest.mark.asyncio
    async def test_empty_email_returns_none(self) -> None:
        """Empty string email returns None without DB call."""
        svc = _make_service()

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
        ) as mock_acquire:
            result = await svc.find_integration_by_email("")

        assert result is None
        mock_acquire.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_email_returns_none(self) -> None:
        """None email returns None without DB call."""
        svc = _make_service()

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
        ) as mock_acquire:
            result = await svc.find_integration_by_email(None)  # type: ignore[arg-type]

        assert result is None
        mock_acquire.assert_not_called()

    @pytest.mark.asyncio
    async def test_lookup_uses_provider_parent_integration_types(self) -> None:
        svc = _make_service()
        capture, session_gen = _capturing_session_gen([])

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=session_gen,
        ):
            result = await svc.find_integration_by_email("organiser@example.com")

        assert result is None
        stmt = _compile_sql(capture.statements[0])
        assert "'google'" in stmt
        assert "'microsoft'" in stmt
        assert "'google_calendar'" not in stmt
        assert "'outlook_calendar'" not in stmt

    @pytest.mark.asyncio
    async def test_email_lookup_can_be_limited_to_event_provider_parent(self) -> None:
        svc = _make_service()
        capture, session_gen = _capturing_session_gen([])

        with patch(
            "src.service.integrations.transcripts.service.acquire_org_async_session",
            side_effect=session_gen,
        ):
            result = await svc.find_integration_by_email(
                "organiser@example.com",
                integration_type=IntegrationType.GOOGLE,
            )

        assert result is None
        stmt = _compile_sql(capture.statements[0])
        assert "'google'" in stmt
        assert "'microsoft'" not in stmt
