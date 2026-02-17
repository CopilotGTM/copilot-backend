"""Teams transcript provider - fetches transcripts via Microsoft Graph API.

Uses Nango for OAuth token management (token storage + refresh) but calls
the Microsoft Graph API directly to avoid proxy request quota limits.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any
from uuid import UUID

import httpx
from src.domain.models.organisation_tables.sync.meeting_intelligence import TranscriptSegment
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient, NangoConnectionError

logger = get_logger(__name__)

PROVIDER_CONFIG_KEY = "outlook-bflm"
_GRAPH_API_BASE = "https://graph.microsoft.com"


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


async def _get_access_token(nango: NangoClient, connection_id: str) -> str:
    """Retrieve a fresh OAuth access token from Nango.

    Uses Nango's connection API (not proxy) to get/refresh the token.
    """
    connection = await nango.get_connection(
        provider=PROVIDER_CONFIG_KEY,
        connection_id=connection_id,
        force_refresh=True,
    )
    token = connection.get("credentials", {}).get("access_token")
    if not token:
        raise NangoConnectionError(f"No access_token for connection {connection_id}")
    return token


# ---------------------------------------------------------------------------
# Direct Graph API helper
# ---------------------------------------------------------------------------


async def _graph_api_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET JSON from Microsoft Graph API.

    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    url = f"{_GRAPH_API_BASE}{path}"
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class TeamsTranscriptProvider:
    """Fetches transcripts from Microsoft Teams via Graph API."""

    def __init__(self, nango: NangoClient | None = None):
        self._nango = nango or NangoClient()

    async def resolve_meeting_id(self, connection_id: str, join_url: str | None) -> str | None:
        """Resolve actual Teams meeting ID from join URL.

        The join URL contains a thread ID, but Graph API needs the meeting GUID.
        """
        if not join_url:
            return None

        decoded_url = urllib.parse.unquote(join_url)
        params: dict[str, str] = {"$filter": f"JoinWebUrl eq '{decoded_url}'"}

        try:
            token = await _get_access_token(self._nango, connection_id)
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=60.0,
            ) as client:
                resp = await _graph_api_get(client, "/v1.0/me/onlineMeetings", params=params)
            meetings = resp.get("value", [])
            if meetings:
                meeting_id = meetings[0].get("id")
                logger.info(
                    "Resolved Teams meeting ID: %s", meeting_id[:50] if meeting_id else None
                )
                return meeting_id
            logger.warning("No meeting found for join URL")
            return None
        except NangoConnectionError:
            raise
        except Exception as e:
            logger.warning("Failed to resolve Teams meeting ID: %s", e)
            return None

    async def fetch_transcript(self, connection_id: str, meeting_id: str) -> dict | None:
        """Fetch transcript data from Teams API.

        Returns dict with metadata, vtt_content, language - or None if not found.
        Raises NangoConnectionError for auth/connection issues (should retry).
        """
        encoded_meeting_id = urllib.parse.quote(meeting_id, safe="")
        endpoint = f"/v1.0/me/onlineMeetings/{encoded_meeting_id}/transcripts"

        logger.info(
            "Fetching Teams transcripts: connection_id=%s, meeting_id=%s",
            connection_id,
            meeting_id[:50],
        )

        token = await _get_access_token(self._nango, connection_id)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        ) as client:
            # List transcripts
            try:
                resp = await _graph_api_get(client, endpoint)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                error_text = e.response.text[:300].lower()
                if status in (401, 403) or any(
                    x in error_text for x in ("auth", "token", "credential")
                ):
                    raise NangoConnectionError(
                        f"Teams API auth error (HTTP {status})", status_code=status
                    ) from e
                logger.warning("Failed to list transcripts: HTTP %s", status)
                return None

            transcripts = resp.get("value", [])
            if not transcripts:
                logger.info("No transcripts for meeting %s", meeting_id[:50])
                return None

            logger.info("Found %d transcript(s) for meeting", len(transcripts))
            transcript_meta = transcripts[0]
            transcript_id = transcript_meta["id"]

            # Fetch VTT content using the same client
            vtt_content = await self._fetch_vtt_content(client, encoded_meeting_id, transcript_id)

        if not vtt_content:
            return None

        return {
            "metadata": transcript_meta,
            "vtt_content": vtt_content,
            "language": transcript_meta.get("language"),
        }

    async def _fetch_vtt_content(
        self,
        client: httpx.AsyncClient,
        encoded_meeting_id: str,
        transcript_id: str,
    ) -> str | None:
        """Fetch raw VTT content from Teams API directly."""
        encoded_transcript_id = urllib.parse.quote(transcript_id, safe="")
        url = (
            f"{_GRAPH_API_BASE}/v1.0/me/onlineMeetings/{encoded_meeting_id}"
            f"/transcripts/{encoded_transcript_id}/content"
        )

        try:
            response = await client.get(url, params={"$format": "text/vtt"})
            if response.status_code >= 400:
                status = response.status_code
                error_text = response.text[:300].lower()
                if status in (401, 403) or any(
                    x in error_text for x in ("auth", "token", "credential")
                ):
                    raise NangoConnectionError(
                        f"Teams VTT fetch auth error (HTTP {status})",
                        status_code=status,
                    )
                logger.warning("Failed to fetch VTT: HTTP %s", status)
                return None
            logger.info("Fetched VTT content: %d chars", len(response.text))
            return response.text
        except NangoConnectionError:
            raise
        except Exception as e:
            error_msg = str(e).lower()
            if any(x in error_msg for x in ("connection", "timeout")):
                raise NangoConnectionError(
                    f"Teams API connection error: {e}", status_code=None
                ) from e
            logger.warning("Failed to fetch VTT content: %s", e)
            return None

    @staticmethod
    def parse_vtt(vtt_content: str, meeting_id: UUID) -> tuple[str, list[TranscriptSegment]]:
        """Parse VTT content into full transcript and segments.

        VTT format: <v Speaker Name>spoken text</v>
        """
        lines = []
        segments = []
        pattern = re.compile(r"<v ([^>]+)>(.+)</v>")

        for line in vtt_content.split("\n"):
            line = line.strip()
            if not line or line == "WEBVTT" or "-->" in line or line.startswith("NOTE"):
                continue

            match = pattern.match(line)
            if match:
                speaker, text = match.group(1).strip(), match.group(2).strip()
                lines.append(f"{speaker}: {text}")
                segments.append(
                    TranscriptSegment(
                        meeting_id=meeting_id,
                        speaker_name=speaker,
                        segment_index=len(segments),
                        content=text,
                    )
                )
            else:
                lines.append(line)
                segments.append(
                    TranscriptSegment(
                        meeting_id=meeting_id,
                        speaker_name="Unknown",
                        segment_index=len(segments),
                        content=line,
                    )
                )

        return "\n".join(lines), segments
