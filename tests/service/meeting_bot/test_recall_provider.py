"""Tests for RecallMeetingRecordingProvider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.service.meeting_bot.providers.recall_client import (
    RecallMeetingRecordingProvider,
    RecallProviderError,
)


def _json_response(method: str, url: str, data: dict, status_code: int = 200) -> httpx.Response:
    """Build an httpx response object with a request attached for error paths."""
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request(method, url),
    )


@pytest.mark.asyncio
class TestRecallMeetingRecordingProvider:
    async def test_create_session_sends_expected_payload(self) -> None:
        provider = RecallMeetingRecordingProvider(
            api_key="test-key",
            region="us-east-1",
        )
        join_at = datetime.now(UTC) + timedelta(minutes=5)
        response = _json_response(
            "POST",
            "https://us-east-1.recall.ai/api/v1/bot/",
            {"id": "bot_123", "status": "scheduled"},
            status_code=201,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            result = await provider.create_session(
                meeting_url="https://teams.example/join",
                join_at=join_at,
                bot_name="Acme Corp Notetaker",
                metadata={"organisation_id": str(uuid4())},
            )

        assert result["id"] == "bot_123"
        kwargs = mock_client.post.call_args.kwargs
        assert kwargs["json"]["meeting_url"] == "https://teams.example/join"
        assert kwargs["json"]["bot_name"] == "Acme Corp Notetaker"
        assert kwargs["json"]["metadata"]
        assert (
            kwargs["json"]["recording_config"]["transcript"]["provider"]["recallai_streaming"][
                "mode"
            ]
            == "prioritize_accuracy"
        )
        assert kwargs["json"]["automatic_leave"] == {
            "waiting_room_timeout": 600,
            "noone_joined_timeout": 600,
            "in_call_not_recording_timeout": 300,
            "recording_permission_denied_timeout": 30,
        }

    async def test_get_session_retrieves_bot(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        response = _json_response(
            "GET",
            "https://us-east-1.recall.ai/api/v1/bot/bot_123/",
            {"id": "bot_123", "status": "done"},
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            result = await provider.fetch_session("bot_123")

        assert result["status"] == "done"

    async def test_cancel_session_deletes_scheduled_bot(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        response = httpx.Response(
            status_code=204,
            request=httpx.Request("DELETE", "https://us-east-1.recall.ai/api/v1/bot/bot_123/"),
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.delete.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            await provider.cancel_session("bot_123")

        mock_client.delete.assert_awaited_once()

    async def test_update_session_patches_scheduled_bot(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        join_at = datetime.now(UTC) + timedelta(hours=2)
        response = _json_response(
            "PATCH",
            "https://us-east-1.recall.ai/api/v1/bot/bot_123/",
            {"id": "bot_123", "status": "scheduled"},
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.patch.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            result = await provider.update_session(
                "bot_123",
                meeting_url="https://teams.example/join",
                join_at=join_at,
                bot_name="Acme Corp Notetaker",
                metadata={"calendar_event_id": str(uuid4())},
            )

        assert result["id"] == "bot_123"
        mock_client.patch.assert_awaited_once()
        kwargs = mock_client.patch.call_args.kwargs
        assert kwargs["json"]["meeting_url"] == "https://teams.example/join"
        assert kwargs["json"]["join_at"] == join_at.isoformat()
        assert kwargs["json"]["metadata"]["calendar_event_id"]
        assert kwargs["json"]["automatic_leave"]["waiting_room_timeout"] == 600
        assert kwargs["json"]["automatic_leave"]["in_call_not_recording_timeout"] == 300

    async def test_leave_call_posts_to_leave_endpoint(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        response = _json_response(
            "POST",
            "https://us-east-1.recall.ai/api/v1/bot/bot_123/leave_call/",
            {},
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            await provider.leave_call("bot_123")

        mock_client.post.assert_awaited_once_with(
            "https://us-east-1.recall.ai/api/v1/bot/bot_123/leave_call/"
        )

    async def test_list_sessions_returns_paginated_results(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        first = _json_response(
            "GET",
            "https://us-east-1.recall.ai/api/v1/bot/",
            {
                "results": [{"id": "bot_1"}],
                "next": "https://us-east-1.recall.ai/api/v1/bot/?cursor=abc",
            },
        )
        second = _json_response(
            "GET",
            "https://us-east-1.recall.ai/api/v1/bot/?cursor=abc",
            {"results": [{"id": "bot_2"}], "next": None},
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = [first, second]
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            result = await provider.list_sessions(join_at_after=datetime.now(UTC))

        assert result == [{"id": "bot_1"}, {"id": "bot_2"}]
        assert mock_client.get.await_count == 2

    async def test_extract_transcript_download_url_from_bot_payload(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")

        payload = {
            "id": "bot_123",
            "recordings": [
                {
                    "id": "rec_1",
                    "media_shortcuts": {
                        "transcript": {
                            "id": "transcript_1",
                            "data": {"download_url": "https://download.example/transcript"},
                        }
                    },
                }
            ],
        }

        result = provider.extract_transcript_download(payload)

        assert result == {
            "recording_id": "rec_1",
            "transcript_id": "transcript_1",
            "download_url": "https://download.example/transcript",
        }

    async def test_fetch_transcript_downloads_json_payload(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        transcript_payload = [
            {
                "participant": {"id": 100, "name": "Alice"},
                "words": [{"text": "hello", "start_timestamp": {"relative": 1.0}}],
            }
        ]
        response = _json_response(
            "GET",
            "https://download.example/transcript",
            transcript_payload,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            result = await provider.fetch_transcript("https://download.example/transcript")

        assert result == transcript_payload

    async def test_extract_video_mixed_download_returns_playback_artifact(self) -> None:
        payload = {
            "recordings": [
                {
                    "id": "recording_123",
                    "media_shortcuts": {
                        "video_mixed": {
                            "id": "video_123",
                            "data": {"download_url": "https://download.example/video.mp4"},
                        }
                    },
                }
            ]
        }

        result = RecallMeetingRecordingProvider.extract_video_mixed_download(payload)

        assert result == {
            "recording_id": "recording_123",
            "video_id": "video_123",
            "download_url": "https://download.example/video.mp4",
        }

    async def test_create_session_raises_provider_error_on_http_failure(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        response = _json_response(
            "POST",
            "https://us-east-1.recall.ai/api/v1/bot/",
            {"detail": "bad request"},
            status_code=400,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            with pytest.raises(RecallProviderError) as exc_info:
                await provider.create_session(
                    meeting_url="https://teams.example/join",
                    join_at=datetime.now(UTC) + timedelta(minutes=5),
                    metadata={},
                )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "bad request"

    async def test_provider_error_preserves_recall_code_and_detail(self) -> None:
        provider = RecallMeetingRecordingProvider(api_key="test-key", region="us-east-1")
        response = _json_response(
            "PATCH",
            "https://us-east-1.recall.ai/api/v1/bot/bot_123/",
            {
                "code": "update_bot_failed",
                "detail": "Not enough time to launch new bot",
            },
            status_code=400,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.patch.return_value = response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_client_cls.return_value = mock_client

            with pytest.raises(RecallProviderError) as exc_info:
                await provider.update_session(
                    "bot_123",
                    meeting_url="https://teams.example/join",
                    join_at=datetime.now(UTC) + timedelta(minutes=5),
                    metadata={},
                )

        assert exc_info.value.status_code == 400
        assert exc_info.value.code == "update_bot_failed"
        assert exc_info.value.detail == "Not enough time to launch new bot"
