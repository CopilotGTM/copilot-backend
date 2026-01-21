"""Teams transcript provider - handles Microsoft Graph API interactions."""

from __future__ import annotations

import os
import re
import urllib.parse
from uuid import UUID

import httpx
from src.domain.models.organisation_tables.sync.meeting_intelligence import TranscriptSegment
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient, NangoConnectionError

logger = get_logger(__name__)

PROVIDER_CONFIG_KEY = "outlook-bflm"


class TeamsTranscriptProvider:
    """Fetches transcripts from Microsoft Teams via Graph API."""

    def __init__(self, nango: NangoClient | None = None):
        self.nango = nango or NangoClient()

    async def resolve_meeting_id(self, connection_id: str, join_url: str | None) -> str | None:
        """Resolve actual Teams meeting ID from join URL.

        The join URL contains a thread ID, but Graph API needs the meeting GUID.
        """
        if not join_url:
            return None

        decoded_url = urllib.parse.unquote(join_url)
        params = {"$filter": f"JoinWebUrl eq '{decoded_url}'"}

        try:
            resp = await self.nango.proxy_request(
                provider_config_key=PROVIDER_CONFIG_KEY,
                connection_id=connection_id,
                endpoint="/v1.0/me/onlineMeetings",
                params=params,
            )
            meetings = resp.get("value", [])
            if meetings:
                meeting_id = meetings[0].get("id")
                logger.info(
                    "Resolved Teams meeting ID: %s", meeting_id[:50] if meeting_id else None
                )
                return meeting_id
            logger.warning("No meeting found for join URL")
            return None
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

        # List transcripts
        try:
            resp = await self.nango.proxy_request(
                provider_config_key=PROVIDER_CONFIG_KEY,
                connection_id=connection_id,
                endpoint=endpoint,
            )
        except NangoConnectionError:
            raise  # Let caller handle retry
        except Exception as e:
            error_msg = str(e).lower()
            if any(x in error_msg for x in ("connection", "auth", "token")):
                raise NangoConnectionError(
                    f"Teams API connection error: {e}", status_code=None
                ) from e
            logger.warning("Failed to list transcripts: %s", e)
            return None

        transcripts = resp.get("value", [])
        if not transcripts:
            logger.info("No transcripts for meeting %s", meeting_id[:50])
            return None

        logger.info("Found %d transcript(s) for meeting", len(transcripts))
        transcript_meta = transcripts[0]
        transcript_id = transcript_meta["id"]

        # Fetch VTT content
        vtt_content = await self._fetch_vtt_content(
            connection_id, encoded_meeting_id, transcript_id
        )
        if not vtt_content:
            return None

        return {
            "metadata": transcript_meta,
            "vtt_content": vtt_content,
            "language": transcript_meta.get("language"),
        }

    async def _fetch_vtt_content(
        self, connection_id: str, encoded_meeting_id: str, transcript_id: str
    ) -> str | None:
        """Fetch raw VTT content from Teams API."""
        nango_secret_key = os.getenv("NANGO_SECRET_KEY")
        if not nango_secret_key:
            logger.error("NANGO_SECRET_KEY not set")
            return None

        encoded_transcript_id = urllib.parse.quote(transcript_id, safe="")
        content_endpoint = (
            f"/proxy/v1.0/me/onlineMeetings/{encoded_meeting_id}"
            f"/transcripts/{encoded_transcript_id}/content?$format=text/vtt"
        )

        try:
            async with httpx.AsyncClient(
                base_url="https://api.nango.dev",
                headers={"Authorization": f"Bearer {nango_secret_key}"},
                timeout=60.0,
            ) as client:
                response = await client.get(
                    content_endpoint,
                    headers={
                        "Provider-Config-Key": PROVIDER_CONFIG_KEY,
                        "Connection-Id": connection_id,
                    },
                )
                if response.status_code != 200:
                    logger.warning("Failed to fetch VTT: HTTP %s", response.status_code)
                    return None
                logger.info("Fetched VTT content: %d chars", len(response.text))
                return response.text
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
