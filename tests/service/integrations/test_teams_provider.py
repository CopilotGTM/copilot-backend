"""Tests for TeamsTranscriptProvider (direct Graph API calls)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from src.service.integrations.nango_client import NangoConnectionError
from src.service.integrations.transcripts.teams_provider import TeamsTranscriptProvider

# =============================================================================
# Helpers
# =============================================================================

_FAKE_TOKEN_RESPONSE = {"credentials": {"access_token": "test-token"}}


def _make_provider() -> TeamsTranscriptProvider:
    """Create a TeamsTranscriptProvider with mocked Nango client."""
    provider = TeamsTranscriptProvider.__new__(TeamsTranscriptProvider)
    provider._nango = MagicMock()
    provider._nango.get_connection = AsyncMock(return_value=_FAKE_TOKEN_RESPONSE)
    return provider


def _mock_response(
    status_code: int = 200, json_data: dict | None = None, text: str = ""
) -> MagicMock:
    """Build a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


# =============================================================================
# TestResolveMeetingId
# =============================================================================


class TestResolveMeetingId:
    """Tests for TeamsTranscriptProvider.resolve_meeting_id."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_id(self) -> None:
        provider = _make_provider()

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            return_value={"value": [{"id": "teams-meeting-guid-123"}]},
        ) as mock_get:
            result = await provider.resolve_meeting_id("conn-1", "https://teams.link/join")

        assert result == "teams-meeting-guid-123"
        mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_join_url_returns_none(self) -> None:
        provider = _make_provider()

        result = await provider.resolve_meeting_id("conn-1", None)

        assert result is None
        # Should not even attempt token retrieval
        provider._nango.get_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_join_url_returns_none(self) -> None:
        provider = _make_provider()

        result = await provider.resolve_meeting_id("conn-1", "")

        assert result is None

    @pytest.mark.asyncio
    async def test_no_meetings_found_returns_none(self) -> None:
        provider = _make_provider()

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            return_value={"value": []},
        ):
            result = await provider.resolve_meeting_id("conn-1", "https://teams.link/join")

        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self) -> None:
        provider = _make_provider()

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network error"),
        ):
            result = await provider.resolve_meeting_id("conn-1", "https://teams.link/join")

        assert result is None

    @pytest.mark.asyncio
    async def test_nango_connection_error_propagates(self) -> None:
        provider = _make_provider()

        with (
            patch(
                "src.service.integrations.transcripts.teams_provider._graph_api_get",
                new_callable=AsyncMock,
                side_effect=NangoConnectionError("auth failed"),
            ),
            pytest.raises(NangoConnectionError),
        ):
            await provider.resolve_meeting_id("conn-1", "https://teams.link/join")

    @pytest.mark.asyncio
    async def test_nango_token_error_propagates(self) -> None:
        """NangoConnectionError from token retrieval propagates."""
        provider = _make_provider()
        provider._nango.get_connection = AsyncMock(
            side_effect=NangoConnectionError("Connection not found")
        )

        with pytest.raises(NangoConnectionError):
            await provider.resolve_meeting_id("conn-1", "https://teams.link/join")

    @pytest.mark.asyncio
    async def test_url_decoded_for_filter(self) -> None:
        """Encoded join URL is decoded before filtering."""
        provider = _make_provider()
        encoded_url = "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_abc"

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            return_value={"value": [{"id": "meeting-id"}]},
        ) as mock_get:
            await provider.resolve_meeting_id("conn-1", encoded_url)

        # Verify the decoded URL was used in the filter param
        call_args = mock_get.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params", {})
        assert "19:meeting_abc" in params.get("$filter", "")


# =============================================================================
# TestFetchTranscript
# =============================================================================


class TestFetchTranscript:
    """Tests for TeamsTranscriptProvider.fetch_transcript."""

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        provider = _make_provider()
        vtt = "WEBVTT\n\n00:00.000 --> 00:01.000\n<v Alice>Hello</v>"

        async def fake_get(client, path, params=None):
            return {
                "value": [{"id": "transcript-1", "language": "en-US"}],
            }

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            side_effect=fake_get,
        ):
            provider._fetch_vtt_content = AsyncMock(return_value=vtt)  # type: ignore[method-assign]
            result = await provider.fetch_transcript("conn-1", "meeting-id-123")

        assert result is not None
        assert result["vtt_content"] == vtt
        assert result["language"] == "en-US"
        assert result["metadata"]["id"] == "transcript-1"

    @pytest.mark.asyncio
    async def test_no_transcripts_returns_none(self) -> None:
        provider = _make_provider()

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            return_value={"value": []},
        ):
            result = await provider.fetch_transcript("conn-1", "meeting-id-123")

        assert result is None

    @pytest.mark.asyncio
    async def test_vtt_content_none_returns_none(self) -> None:
        provider = _make_provider()

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            return_value={"value": [{"id": "transcript-1", "language": "en"}]},
        ):
            provider._fetch_vtt_content = AsyncMock(return_value=None)  # type: ignore[method-assign]
            result = await provider.fetch_transcript("conn-1", "meeting-id-123")

        assert result is None

    @pytest.mark.asyncio
    async def test_auth_error_raises_nango_connection_error(self) -> None:
        """401/403 from Graph API raises NangoConnectionError."""
        provider = _make_provider()
        error_resp = _mock_response(401, text="unauthorized")

        with (
            patch(
                "src.service.integrations.transcripts.teams_provider._graph_api_get",
                new_callable=AsyncMock,
                side_effect=httpx.HTTPStatusError(
                    message="HTTP 401", request=MagicMock(), response=error_resp
                ),
            ),
            pytest.raises(NangoConnectionError, match="auth error"),
        ):
            await provider.fetch_transcript("conn-1", "meeting-id-123")

    @pytest.mark.asyncio
    async def test_non_auth_http_error_returns_none(self) -> None:
        """Non-auth HTTP error (e.g. 500) returns None."""
        provider = _make_provider()
        error_resp = _mock_response(500, text="internal server error")

        with patch(
            "src.service.integrations.transcripts.teams_provider._graph_api_get",
            new_callable=AsyncMock,
            side_effect=httpx.HTTPStatusError(
                message="HTTP 500", request=MagicMock(), response=error_resp
            ),
        ):
            result = await provider.fetch_transcript("conn-1", "meeting-id-123")

        assert result is None

    @pytest.mark.asyncio
    async def test_nango_token_error_propagates(self) -> None:
        provider = _make_provider()
        provider._nango.get_connection = AsyncMock(
            side_effect=NangoConnectionError("token refresh failed")
        )

        with pytest.raises(NangoConnectionError):
            await provider.fetch_transcript("conn-1", "meeting-id-123")


# =============================================================================
# TestParseVtt
# =============================================================================


class TestParseVtt:
    """Tests for TeamsTranscriptProvider.parse_vtt."""

    def test_happy_path(self) -> None:
        meeting_id = uuid4()
        vtt = (
            "WEBVTT\n"
            "\n"
            "00:00:00.000 --> 00:00:05.000\n"
            "<v Alice Smith>Hello everyone</v>\n"
            "\n"
            "00:00:05.000 --> 00:00:10.000\n"
            "<v Bob Jones>Hi there</v>\n"
        )

        full_text, segments = TeamsTranscriptProvider.parse_vtt(vtt, meeting_id)

        assert "Alice Smith: Hello everyone" in full_text
        assert "Bob Jones: Hi there" in full_text
        assert len(segments) == 2
        assert segments[0].speaker_name == "Alice Smith"
        assert segments[0].content == "Hello everyone"
        assert segments[0].segment_index == 0
        assert segments[1].speaker_name == "Bob Jones"
        assert segments[1].content == "Hi there"
        assert segments[1].segment_index == 1

    def test_line_without_speaker_tag(self) -> None:
        """Lines without <v> tags are treated as Unknown speaker."""
        meeting_id = uuid4()
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nSome plain text line\n"

        full_text, segments = TeamsTranscriptProvider.parse_vtt(vtt, meeting_id)

        assert len(segments) == 1
        assert segments[0].speaker_name == "Unknown"
        assert segments[0].content == "Some plain text line"

    def test_skips_webvtt_header_and_timestamps(self) -> None:
        meeting_id = uuid4()
        vtt = (
            "WEBVTT\n"
            "\n"
            "NOTE this is a comment\n"
            "\n"
            "00:00:00.000 --> 00:00:05.000\n"
            "<v Speaker>Content</v>\n"
        )

        full_text, segments = TeamsTranscriptProvider.parse_vtt(vtt, meeting_id)

        assert len(segments) == 1
        assert segments[0].content == "Content"

    def test_empty_vtt_returns_empty(self) -> None:
        meeting_id = uuid4()

        full_text, segments = TeamsTranscriptProvider.parse_vtt("WEBVTT\n", meeting_id)

        assert full_text == ""
        assert segments == []

    def test_multiple_segments_sequential_index(self) -> None:
        meeting_id = uuid4()
        vtt = (
            "WEBVTT\n"
            "\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "<v A>Line 1</v>\n"
            "\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "<v B>Line 2</v>\n"
            "\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "<v A>Line 3</v>\n"
        )

        _, segments = TeamsTranscriptProvider.parse_vtt(vtt, meeting_id)

        assert len(segments) == 3
        assert [s.segment_index for s in segments] == [0, 1, 2]
        assert [s.speaker_name for s in segments] == ["A", "B", "A"]
