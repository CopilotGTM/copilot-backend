"""Recall.ai meeting recording provider adapter.

This wrapper intentionally mirrors only the small Recall surface the backend
uses today: create/update a bot, fetch bot state, cancel or remove a bot, list
upcoming bots for reconciliation, and fetch transcript artifacts. Keeping it
narrow makes provider behavior easier to reason about in worker retries and tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

RECALL_RECONCILIATION_MAX_PAGES = 2
RECALL_RECONCILIATION_MAX_BOTS = 100


class RecallProviderError(RuntimeError):
    """Raised when a Recall API request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        response_body: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.response_body = response_body


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

    @staticmethod
    def _recording_config() -> dict[str, Any]:
        """Build the Recall recording options required by transcript ingestion."""
        return {
            "transcript": {
                "provider": {
                    # The downstream transcript fetch path expects Recall's
                    # transcript shortcut/download artifact to be present.
                    "recallai_streaming": {
                        "mode": "prioritize_accuracy",
                    }
                }
            }
        }

    @staticmethod
    def _automatic_leave_config() -> dict[str, Any]:
        """Keep empty and bot-only calls from consuming recording capacity.

        Recall treats another notetaker as a participant, so `noone_joined_timeout`
        alone cannot end a call containing two bots. Name and activity heuristics
        complement one another because participant events are less reliable on Meet.
        """
        return {
            "waiting_room_timeout": 600,
            "noone_joined_timeout": 600,
            "in_call_not_recording_timeout": 300,
            "recording_permission_denied_timeout": 30,
            "bot_detection": {
                "using_participant_events": {
                    "activate_after": 0,
                    "timeout": 600,
                },
                "using_participant_names": {
                    "matches": [
                        "notetaker",
                        "note taker",
                        "recorder",
                        "copilot",
                        "otter",
                        "fireflies",
                        "fathom",
                        "read.ai",
                        "grain",
                        "fellow",
                        "tl;dv",
                    ],
                    "activate_after": 0,
                    "timeout": 600,
                },
            },
        }

    async def create_session(
        self,
        *,
        meeting_url: str,
        join_at: datetime | None,
        metadata: dict[str, Any],
        bot_name: str = "Notetaker",
        recording_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a bot for one meeting URL.

        Metadata is stored by Recall and reused by webhook processing to recover the
        tenant boundary even if API payloads are ambiguous.
        """
        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "metadata": metadata,
            "recording_config": recording_config or self._recording_config(),
            "automatic_leave": self._automatic_leave_config(),
        }
        # Recall dispatches immediately when join_at is omitted. This is its
        # documented ad-hoc path for meetings discovered less than ten minutes out.
        if join_at is not None:
            payload["join_at"] = join_at.isoformat()
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

    async def update_session(
        self,
        provider_session_id: str,
        *,
        meeting_url: str,
        join_at: datetime,
        metadata: dict[str, Any],
        bot_name: str = "Notetaker",
        recording_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a scheduled bot before Recall starts dispatching it."""
        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "join_at": join_at.isoformat(),
            "metadata": metadata,
            "recording_config": recording_config or self._recording_config(),
            "automatic_leave": self._automatic_leave_config(),
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            response = await client.patch(
                f"{self.base_url}/bot/{provider_session_id}/", json=payload
            )
        return self._json_or_raise(response, expected_status={200})

    async def cancel_session(self, provider_session_id: str) -> None:
        """Cancel one provider session when downstream cancellation flows are triggered."""
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            response = await client.delete(f"{self.base_url}/bot/{provider_session_id}/")
        if response.status_code != 204:
            self._raise_response_error(response, "Recall delete failed")

    async def leave_call(self, provider_session_id: str) -> None:
        """Remove a bot that is already joining or in-call."""
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            response = await client.post(f"{self.base_url}/bot/{provider_session_id}/leave_call/")
        self._json_or_raise(response, expected_status={200})

    async def list_sessions(
        self,
        *,
        join_at_after: datetime | None = None,
        meeting_url: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """List Recall bots for reconciliation and manual dedupe/adoption."""
        params: dict[str, str] = {}
        if join_at_after is not None:
            params["join_at_after"] = join_at_after.isoformat()
        if meeting_url:
            params["meeting_url"] = meeting_url
        for key, value in (metadata or {}).items():
            params[f"metadata__{key}"] = value

        bots: list[dict[str, Any]] = []
        pages_read = 0
        next_url: str | None = f"{self.base_url}/bot/"
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            # Reconciliation is a safety lookup, not a workspace export. Bound the
            # provider walk so a broad legacy query cannot pin a worker indefinitely.
            while next_url and pages_read < RECALL_RECONCILIATION_MAX_PAGES:
                pages_read += 1
                response = await client.get(
                    next_url, params=params if next_url.endswith("/bot/") else None
                )
                data = self._json_or_raise(response, expected_status={200})
                if isinstance(data, list):
                    bots.extend(item for item in data if isinstance(item, dict))
                    break
                bots.extend(item for item in data.get("results", []) if isinstance(item, dict))
                candidate_next_url = data.get("next")
                next_url = (
                    candidate_next_url
                    if isinstance(candidate_next_url, str)
                    and candidate_next_url.startswith(f"{self.base_url}/bot/")
                    else None
                )
                params = {}
                if len(bots) >= RECALL_RECONCILIATION_MAX_BOTS:
                    break
        return bots[:RECALL_RECONCILIATION_MAX_BOTS]

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
    def extract_video_mixed_download(bot_payload: dict[str, Any]) -> dict[str, str] | None:
        """Return the first downloadable mixed-video artifact from a bot payload."""
        for recording in bot_payload.get("recordings", []) or []:
            media_shortcuts = recording.get("media_shortcuts") or {}
            video = media_shortcuts.get("video_mixed") or {}
            video_id = video.get("id")
            data = video.get("data") or {}
            download_url = data.get("download_url")
            if video_id and download_url:
                return {
                    "recording_id": recording.get("id"),
                    "video_id": video_id,
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
            RecallMeetingRecordingProvider._raise_response_error(response, "Recall request failed")
        if not response.content:
            return {}
        return response.json()

    @staticmethod
    def _raise_response_error(response: httpx.Response, prefix: str) -> None:
        """Raise provider errors with Recall's structured body preserved."""
        body: Any
        code: str | None = None
        detail: str | None = None
        try:
            body = response.json()
        except ValueError:
            body = response.text[:300]
        if isinstance(body, dict):
            raw_code = body.get("code")
            raw_detail = body.get("detail")
            code = raw_code if isinstance(raw_code, str) else None
            detail = raw_detail if isinstance(raw_detail, str) else None
        raise RecallProviderError(
            f"{prefix}: HTTP {response.status_code} {response.text[:300]}",
            status_code=response.status_code,
            code=code,
            detail=detail,
            response_body=body,
        )
