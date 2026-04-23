"""Tests for RecallMeetingRecordingProvider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from src.service.integrations.meeting_recording.providers.recall_provider import (
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
                bot_name="CopilotGTM Notetaker",
                metadata={"organisation_id": str(uuid4())},
            )

        assert result["id"] == "bot_123"
        kwargs = mock_client.post.call_args.kwargs
        assert kwargs["json"]["meeting_url"] == "https://teams.example/join"
        assert kwargs["json"]["bot_name"] == "CopilotGTM Notetaker"
        assert kwargs["json"]["metadata"]
        assert (
            kwargs["json"]["recording_config"]["transcript"]["provider"]["recallai_streaming"][
                "mode"
            ]
            == "prioritize_accuracy"
        )

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

            with pytest.raises(RecallProviderError):
                await provider.create_session(
                    meeting_url="https://teams.example/join",
                    join_at=datetime.now(UTC) + timedelta(minutes=5),
                    metadata={},
                )
