"""Tests for GoogleMeetTranscriptProvider and Google Meet routing in TranscriptService."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from src.service.integrations.nango_client import NangoConnectionError
from src.service.integrations.transcripts.google_meet_provider import (
    GoogleMeetTranscriptProvider,
    _extract_display_name,
)
from src.service.integrations.transcripts.service import (
    TranscriptFetchResult,
    TranscriptService,
)

# =============================================================================
# Helpers
# =============================================================================


@dataclass
class FakeIntegration:
    integration_id: UUID = field(default_factory=uuid4)
    integration_email: str = "user@example.com"
    connection_id: str | None = "conn-1"
    is_active: bool = True
    integration_type: str = "GOOGLE_CALENDAR"
    organisation_id: UUID = field(default_factory=uuid4)


@dataclass
class FakeCalendarEvent:
    event_id: UUID = field(default_factory=uuid4)
    organisation_id: UUID = field(default_factory=uuid4)
    organiser_email: str = "organiser@example.com"
    online_meeting_id: str | None = "abc-defg-hij"
    online_meeting_provider: str = "googleMeet"
    conference_link: str | None = None
    integration_id: UUID | None = None


@dataclass
class FakeMeeting:
    meeting_id: UUID = field(default_factory=uuid4)
    calendar_event_id: UUID = field(default_factory=uuid4)
    status: str = "pending"
    transcript_fetch_attempts: int = 0


def _make_service(org_id: UUID | None = None) -> TranscriptService:
    svc = TranscriptService.__new__(TranscriptService)
    svc.organisation_id = org_id or uuid4()
    svc._teams = MagicMock()
    svc._google_meet = MagicMock()
    return svc


# =============================================================================
# TestResolveConferenceRecord
# =============================================================================


def _make_provider() -> GoogleMeetTranscriptProvider:
    """Create a GoogleMeetTranscriptProvider bypassing __init__ env check."""
    provider = GoogleMeetTranscriptProvider.__new__(GoogleMeetTranscriptProvider)
    provider._nango_secret_key = "test-secret"
    return provider


class TestResolveConferenceRecord:
    """Tests for GoogleMeetTranscriptProvider.resolve_conference_record."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_name(self) -> None:
        provider = _make_provider()
        provider._meet_api_get = AsyncMock(
            return_value={
                "conferenceRecords": [{"name": "conferenceRecords/abc123"}],
            }
        )

        result = await provider.resolve_conference_record("conn-1", "abc-defg-hij")

        assert result == "conferenceRecords/abc123"
        provider._meet_api_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_records_returns_none(self) -> None:
        provider = _make_provider()
        provider._meet_api_get = AsyncMock(return_value={"conferenceRecords": []})

        result = await provider.resolve_conference_record("conn-1", "abc-defg-hij")

        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self) -> None:
        provider = _make_provider()
        provider._meet_api_get = AsyncMock(side_effect=RuntimeError("timeout"))

        result = await provider.resolve_conference_record("conn-1", "abc-defg-hij")

        assert result is None

    @pytest.mark.asyncio
    async def test_nango_connection_error_propagates(self) -> None:
        provider = _make_provider()
        provider._meet_api_get = AsyncMock(side_effect=NangoConnectionError("token expired"))

        with pytest.raises(NangoConnectionError):
            await provider.resolve_conference_record("conn-1", "abc-defg-hij")


# =============================================================================
# TestFetchTranscript
# =============================================================================


class TestFetchTranscript:
    """Tests for GoogleMeetTranscriptProvider.fetch_transcript."""

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        provider = _make_provider()

        async def fake_get(conn_id, path, params=None):
            if "transcripts" in path and "entries" not in path:
                return {
                    "transcripts": [
                        {"name": "conferenceRecords/abc/transcripts/t1", "state": "ENDED"}
                    ]
                }
            if "participants" in path:
                return {
                    "participants": [
                        {
                            "name": "conferenceRecords/abc/participants/p1",
                            "signedinUser": {"displayName": "Alice"},
                        }
                    ]
                }
            if "entries" in path:
                return {
                    "transcriptEntries": [
                        {
                            "text": "Hello world",
                            "participant": "conferenceRecords/abc/participants/p1",
                        }
                    ]
                }
            return {}

        provider._meet_api_get = AsyncMock(side_effect=fake_get)

        result = await provider.fetch_transcript("conn-1", "conferenceRecords/abc")

        assert result is not None
        assert len(result["entries"]) == 1
        assert "conferenceRecords/abc/participants/p1" in result["participant_map"]
        assert result["participant_map"]["conferenceRecords/abc/participants/p1"] == "Alice"

    @pytest.mark.asyncio
    async def test_no_transcripts_returns_none(self) -> None:
        provider = _make_provider()
        provider._meet_api_get = AsyncMock(return_value={"transcripts": []})

        result = await provider.fetch_transcript("conn-1", "conferenceRecords/abc")

        assert result is None

    @pytest.mark.asyncio
    async def test_file_generated_state_accepted(self) -> None:
        """FILE_GENERATED transcript should be accepted like ENDED."""
        provider = GoogleMeetTranscriptProvider.__new__(GoogleMeetTranscriptProvider)

        async def fake_get(conn_id, path, params=None):
            if "transcripts" in path and "entries" not in path:
                return {
                    "transcripts": [
                        {
                            "name": "conferenceRecords/abc/transcripts/t1",
                            "state": "FILE_GENERATED",
                        }
                    ]
                }
            if "participants" in path:
                return {"participants": []}
            if "entries" in path:
                return {"transcriptEntries": []}
            return {}

        provider._meet_api_get = AsyncMock(side_effect=fake_get)

        result = await provider.fetch_transcript("conn-1", "conferenceRecords/abc")

        assert result is not None
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_transcript_started_state_returns_none(self) -> None:
        """Transcript still being generated should return None."""
        provider = _make_provider()
        provider._meet_api_get = AsyncMock(
            return_value={
                "transcripts": [
                    {"name": "conferenceRecords/abc/transcripts/t1", "state": "STARTED"}
                ]
            }
        )

        result = await provider.fetch_transcript("conn-1", "conferenceRecords/abc")

        assert result is None

    @pytest.mark.asyncio
    async def test_nango_connection_error_propagates(self) -> None:
        provider = _make_provider()
        provider._meet_api_get = AsyncMock(side_effect=NangoConnectionError("auth error"))

        with pytest.raises(NangoConnectionError):
            await provider.fetch_transcript("conn-1", "conferenceRecords/abc")


# =============================================================================
# TestParseEntries
# =============================================================================


class TestParseEntries:
    """Tests for GoogleMeetTranscriptProvider.parse_entries."""

    def test_happy_path(self) -> None:
        meeting_id = uuid4()
        entries = [
            {
                "text": "Hello everyone",
                "participant": "conferenceRecords/abc/participants/p1",
            },
            {
                "text": "Hi there",
                "participant": "conferenceRecords/abc/participants/p2",
            },
        ]
        participant_map = {
            "conferenceRecords/abc/participants/p1": "Alice",
            "conferenceRecords/abc/participants/p2": "Bob",
        }

        full_text, segments = GoogleMeetTranscriptProvider.parse_entries(
            entries, participant_map, meeting_id
        )

        assert "Alice: Hello everyone" in full_text
        assert "Bob: Hi there" in full_text
        assert len(segments) == 2
        assert segments[0].speaker_name == "Alice"
        assert segments[0].content == "Hello everyone"
        assert segments[0].segment_index == 0
        assert segments[1].speaker_name == "Bob"
        assert segments[1].segment_index == 1

    def test_unknown_participant_fallback(self) -> None:
        meeting_id = uuid4()
        entries = [{"text": "Mystery", "participant": "unknown-ref"}]

        full_text, segments = GoogleMeetTranscriptProvider.parse_entries(entries, {}, meeting_id)

        assert "Unknown: Mystery" in full_text
        assert segments[0].speaker_name == "Unknown"

    def test_empty_entries_returns_empty(self) -> None:
        meeting_id = uuid4()

        full_text, segments = GoogleMeetTranscriptProvider.parse_entries([], {}, meeting_id)

        assert full_text == ""
        assert segments == []

    def test_empty_text_entries_skipped(self) -> None:
        meeting_id = uuid4()
        entries = [
            {"text": "", "participant": "p1"},
            {"text": "   ", "participant": "p1"},
            {"text": "Real content", "participant": "p1"},
        ]

        full_text, segments = GoogleMeetTranscriptProvider.parse_entries(
            entries, {"p1": "Alice"}, meeting_id
        )

        assert len(segments) == 1
        assert segments[0].content == "Real content"


# =============================================================================
# TestExtractDisplayName
# =============================================================================


class TestExtractDisplayName:
    def test_signed_in_user(self) -> None:
        assert _extract_display_name({"signedinUser": {"displayName": "Alice"}}) == "Alice"

    def test_anonymous_user(self) -> None:
        assert _extract_display_name({"anonymousUser": {"displayName": "Guest"}}) == "Guest"

    def test_phone_user(self) -> None:
        assert _extract_display_name({"phoneUser": {"displayName": "+1-555-1234"}}) == "+1-555-1234"

    def test_fallback_to_unknown(self) -> None:
        assert _extract_display_name({}) == "Unknown"

    def test_empty_display_name_falls_through(self) -> None:
        assert (
            _extract_display_name(
                {"signedinUser": {"displayName": ""}, "anonymousUser": {"displayName": "Anon"}}
            )
            == "Anon"
        )


# =============================================================================
# TestGoogleMeetRouting (TranscriptService integration)
# =============================================================================


class TestGoogleMeetRouting:
    """Tests that TranscriptService routes googleMeet correctly."""

    @pytest.mark.asyncio
    async def test_try_fetch_routes_to_fetch_google_meet(self) -> None:
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_provider="googleMeet")
        integration = FakeIntegration()
        expected = TranscriptFetchResult(success=True, meeting=FakeMeeting())

        svc._fetch_google_meet = AsyncMock(return_value=expected)  # type: ignore[method-assign]

        result = await svc.try_fetch_with_integration(event, integration)  # type: ignore[arg-type]

        assert result == expected
        svc._fetch_google_meet.assert_awaited_once_with(event, integration, None)

    @pytest.mark.asyncio
    async def test_fetch_google_meet_happy_path(self) -> None:
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_id="abc-defg-hij")
        integration = FakeIntegration()
        meeting = FakeMeeting()
        refreshed = FakeMeeting(status="ready")

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._google_meet.resolve_conference_record = AsyncMock(return_value="conferenceRecords/abc")
        svc._google_meet.fetch_transcript = AsyncMock(
            return_value={
                "entries": [{"text": "Hello", "participant": "p1"}],
                "participant_map": {"p1": "Alice"},
                "language": "en-US",
            }
        )
        svc._google_meet.parse_entries = MagicMock(return_value=("Alice: Hello", []))
        svc._save_transcript = AsyncMock()  # type: ignore[method-assign]
        svc._get_meeting = AsyncMock(return_value=refreshed)  # type: ignore[method-assign]

        result = await svc._fetch_google_meet(event, integration, None)  # type: ignore[arg-type]

        assert result.success is True
        assert result.meeting == refreshed
        svc._create_or_get_meeting.assert_awaited_once()
        svc._google_meet.resolve_conference_record.assert_awaited_once()
        svc._google_meet.fetch_transcript.assert_awaited_once()
        svc._save_transcript.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_google_meet_nango_error_retryable(self) -> None:
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = FakeIntegration()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._google_meet.resolve_conference_record = AsyncMock(
            side_effect=NangoConnectionError("token expired")
        )

        result = await svc._fetch_google_meet(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True
        assert "token expired" in (result.error or "")

    @pytest.mark.asyncio
    async def test_fetch_google_meet_generic_error_non_retryable(self) -> None:
        svc = _make_service()
        event = FakeCalendarEvent()
        integration = FakeIntegration()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._google_meet.resolve_conference_record = AsyncMock(
            side_effect=RuntimeError("something broke")
        )

        result = await svc._fetch_google_meet(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is False
        assert "something broke" in (result.error or "")

    @pytest.mark.asyncio
    async def test_fetch_google_meet_no_meeting_code(self) -> None:
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_id=None)
        integration = FakeIntegration()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]

        result = await svc._fetch_google_meet(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is False

    @pytest.mark.asyncio
    async def test_fetch_google_meet_no_conference_record(self) -> None:
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_id="abc-defg-hij")
        integration = FakeIntegration()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._google_meet.resolve_conference_record = AsyncMock(return_value=None)

        result = await svc._fetch_google_meet(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True

    @pytest.mark.asyncio
    async def test_fetch_google_meet_no_transcript_data(self) -> None:
        svc = _make_service()
        event = FakeCalendarEvent(online_meeting_id="abc-defg-hij")
        integration = FakeIntegration()
        meeting = FakeMeeting()

        svc._create_or_get_meeting = AsyncMock(return_value=meeting)  # type: ignore[method-assign]
        svc._google_meet.resolve_conference_record = AsyncMock(return_value="conferenceRecords/abc")
        svc._google_meet.fetch_transcript = AsyncMock(return_value=None)

        result = await svc._fetch_google_meet(event, integration, None)  # type: ignore[arg-type]

        assert result.success is False
        assert result.should_retry is True
