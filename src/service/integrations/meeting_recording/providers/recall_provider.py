"""Recall.ai meeting recording provider adapter.

This wrapper intentionally mirrors only the small Recall surface the backend
uses today: create a bot, fetch bot state, cancel a bot, and fetch transcript
artifacts. Keeping it narrow makes provider behavior easier to reason about in
worker retries and tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


class RecallProviderError(RuntimeError):
    """Raised when a Recall API request fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RecallMeetingRecordingProvider:
    """Thin client for the subset of Recall used by recording dispatch."""

    def __init__(
        self,
        *,
        api_key: str,
        region: str = "us-east-1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.region = region
        self.timeout = timeout
        self.base_url = f"https://{region}.recall.ai/api/v1"

    def _headers(self) -> dict[str, str]:
        """Build fixed headers for authenticated JSON calls."""
        # Recall expects the raw key in the Authorization header in this integration.
        return {
            "Authorization": self.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }

    async def create_session(
        self,
        *,
        meeting_url: str,
        join_at: datetime,
        metadata: dict[str, Any],
        bot_name: str = "CopilotGTM Notetaker",
    ) -> dict[str, Any]:
        """Create a bot for one meeting URL.

        Metadata is stored by Recall and reused by webhook processing to recover the
        tenant boundary even if API payloads are ambiguous.
        """
        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "join_at": join_at.isoformat(),
            "metadata": metadata,
            "recording_config": {
                "transcript": {
                    "provider": {
                        # The downstream transcript fetch path expects Recall's
                        # transcript shortcut/download artifact to be present.
                        "recallai_streaming": {
                            "mode": "prioritize_accuracy",
                        }
                    }
                }
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            # Use a short-lived client per call to avoid carrying shared TCP state
            # across unrelated dispatch tasks and keep retries safe.
            response = await client.post(f"{self.base_url}/bot/", json=payload)
        return self._json_or_raise(response, expected_status={200, 201})

    async def fetch_session(self, provider_session_id: str) -> dict[str, Any]:
        """Read the latest bot state after webhook indicates transcript completion."""
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            response = await client.get(f"{self.base_url}/bot/{provider_session_id}/")
        return self._json_or_raise(response, expected_status={200})

    async def cancel_session(self, provider_session_id: str) -> None:
        """Cancel one provider session when downstream cancellation flows are triggered."""
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            response = await client.delete(f"{self.base_url}/bot/{provider_session_id}/")
        if response.status_code != 204:
            raise RecallProviderError(
                f"Recall delete failed: HTTP {response.status_code} {response.text[:300]}",
                status_code=response.status_code,
            )

    async def fetch_transcript(self, download_url: str) -> list[dict[str, Any]]:
        """Download transcript artifact JSON from a Recall-provided URL."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(download_url)
        return self._json_or_raise(response, expected_status={200})

    @staticmethod
    def extract_transcript_download(bot_payload: dict[str, Any]) -> dict[str, str] | None:
        """Return the first downloadable transcript artifact from a bot payload."""
        for recording in bot_payload.get("recordings", []) or []:
            media_shortcuts = recording.get("media_shortcuts") or {}
            transcript = media_shortcuts.get("transcript") or {}
            transcript_id = transcript.get("id")
            data = transcript.get("data") or {}
            download_url = data.get("download_url")
            if transcript_id and download_url:
                return {
                    "recording_id": recording.get("id"),
                    "transcript_id": transcript_id,
                    "download_url": download_url,
                }
        return None

    @staticmethod
    def _json_or_raise(
        response: httpx.Response,
        *,
        expected_status: set[int],
    ) -> Any:
        """Normalize Recall responses and raise a typed error on status mismatches."""
        # Failing fast here keeps retry policy centralized in the caller tasks.
        if response.status_code not in expected_status:
            raise RecallProviderError(
                f"Recall request failed: HTTP {response.status_code} {response.text[:300]}",
                status_code=response.status_code,
            )
        return response.json()
