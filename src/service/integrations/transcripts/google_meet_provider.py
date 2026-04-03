"""Google Meet transcript provider - fetches transcripts via Google Meet REST API.

Uses custom OAuth via token_resolution for token management.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    TranscriptSegment,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)

logger = get_logger(__name__)

_MEET_API_BASE = "https://meet.googleapis.com"
_ENTRIES_PAGE_SIZE = 100
_MAX_PAGES = 50


class TranscriptAuthError(Exception):
    """Raised when transcript fetch fails due to auth issues (should retry with next integration)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _meet_api_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET request to the Google Meet API directly."""
    url = f"{_MEET_API_BASE}{path}"
    response = await client.get(url, params=params or {})

    if response.status_code == 403:
        raise TranscriptAuthError(
            "Google Meet scope not authorized — user must re-authorize "
            "with meetings.space.readonly",
            status_code=403,
        )

    if response.status_code >= 400:
        error_text = response.text[:300]
        if any(x in error_text.lower() for x in ("auth", "token", "credential", "unauthenticated")):
            raise TranscriptAuthError(
                f"Google Meet API auth error: {error_text}",
                status_code=response.status_code,
            )
        raise RuntimeError(f"Google Meet API error {response.status_code}: {error_text}")

    return response.json()


class GoogleMeetTranscriptProvider:
    """Fetches transcripts from Google Meet via the Meet REST API v2."""

    def __init__(self, token_info: IntegrationTokenInfo | None = None):
        self._token_info = token_info

    def with_token_info(self, token_info: IntegrationTokenInfo) -> GoogleMeetTranscriptProvider:
        """Return a copy configured with the given token info."""
        self._token_info = token_info
        return self

    async def _get_token(self) -> str:
        if not self._token_info:
            raise RuntimeError("token_info not set — call with_token_info() first")
        return await get_access_token(self._token_info)

    async def resolve_conference_record(
        self,
        connection_id: str,  # noqa: ARG002
        meeting_code: str,
    ) -> str | None:
        """Resolve a meeting code to a conference record name."""
        try:
            token = await self._get_token()
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=60.0,
            ) as client:
                resp = await _meet_api_get(
                    client,
                    "/v2/conferenceRecords",
                    params={"filter": f'space.meeting_code="{meeting_code}"'},
                )
            records = resp.get("conferenceRecords", [])
            if not records:
                logger.info("No conference record for meeting code %s", meeting_code)
                return None
            name = records[0].get("name")
            logger.info("Resolved conference record: %s", name)
            return name
        except TranscriptAuthError:
            raise
        except Exception as e:
            logger.warning("Failed to resolve conference record: %s", e)
            return None

    async def fetch_transcript(
        self,
        connection_id: str,  # noqa: ARG002
        conference_record_name: str,
    ) -> dict[str, Any] | None:
        """Fetch transcript data for a conference record.

        Returns dict with entries, participant_map, language — or None if
        no usable transcript exists.
        """
        token = await self._get_token()
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        ) as client:
            resp = await _meet_api_get(client, f"/v2/{conference_record_name}/transcripts")
            transcripts = resp.get("transcripts", [])
            if not transcripts:
                logger.info("No transcripts for %s", conference_record_name)
                return None

            transcript = None
            for t in transcripts:
                state = t.get("state", "")
                if state in ("ENDED", "FILE_GENERATED"):
                    transcript = t
                    break

            if transcript is None:
                logger.info(
                    "No finished transcript for %s (states: %s)",
                    conference_record_name,
                    [t.get("state") for t in transcripts],
                )
                return None

            transcript_name = transcript["name"]
            logger.info("Using transcript %s (state=%s)", transcript_name, transcript.get("state"))

            participant_map = await self._fetch_participants(client, conference_record_name)
            entries = await self._fetch_entries(client, transcript_name)

        return {
            "entries": entries,
            "participant_map": participant_map,
            "language": transcript.get("language"),
        }

    async def _fetch_participants(
        self, client: httpx.AsyncClient, conference_record_name: str
    ) -> dict[str, str]:
        participant_map: dict[str, str] = {}
        page_token: str | None = None

        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {}
            if page_token:
                params["pageToken"] = page_token

            resp = await _meet_api_get(
                client,
                f"/v2/{conference_record_name}/participants",
                params=params,
            )

            for p in resp.get("participants", []):
                name = p.get("name", "")
                display = _extract_display_name(p)
                if name:
                    participant_map[name] = display

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        logger.info("Fetched %d participants", len(participant_map))
        return participant_map

    async def _fetch_entries(
        self, client: httpx.AsyncClient, transcript_name: str
    ) -> list[dict[str, Any]]:
        all_entries: list[dict[str, Any]] = []
        page_token: str | None = None

        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {"pageSize": str(_ENTRIES_PAGE_SIZE)}
            if page_token:
                params["pageToken"] = page_token

            resp = await _meet_api_get(
                client,
                f"/v2/{transcript_name}/entries",
                params=params,
            )

            entries = resp.get("transcriptEntries", [])
            all_entries.extend(entries)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        logger.info("Fetched %d transcript entries", len(all_entries))
        return all_entries

    @staticmethod
    def parse_entries(
        entries: list[dict[str, Any]],
        participant_map: dict[str, str],
        meeting_id: UUID,
    ) -> tuple[str, list[TranscriptSegment]]:
        lines: list[str] = []
        segments: list[TranscriptSegment] = []

        for entry in entries:
            text = (entry.get("text") or "").strip()
            if not text:
                continue

            participant_ref = entry.get("participant", "")
            speaker = participant_map.get(participant_ref, "Unknown")

            lines.append(f"{speaker}: {text}")
            segments.append(
                TranscriptSegment(
                    meeting_id=meeting_id,
                    speaker_name=speaker,
                    segment_index=len(segments),
                    content=text,
                )
            )

        return "\n".join(lines), segments


def _extract_display_name(participant: dict[str, Any]) -> str:
    for key in ("signedinUser", "anonymousUser", "phoneUser"):
        user_info = participant.get(key)
        if isinstance(user_info, dict):
            name = user_info.get("displayName", "").strip()
            if name:
                return name
    return "Unknown"
