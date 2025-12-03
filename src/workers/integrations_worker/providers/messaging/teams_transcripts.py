"""
Teams Transcript Provider.

Fetches meeting transcripts from Microsoft Teams via Microsoft Graph API.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from src.domain.models.organisation_tables.sync.conversation_intelligence import (
    CICall,
    CICallMedium,
    CICallRecordingStatus,
    CICallSpeaker,
    CICallTranscriptSegment,
    CISpeakerRole,
    TranscriptContentFormat,
)
from src.service.integrations.nango_client import NangoClient


class TeamsTranscriptProvider:
    """Fetches and parses Teams meeting transcripts."""

    def __init__(self, nango: NangoClient):
        self.nango = nango

    async def fetch_transcript(
        self,
        connection_id: str,
        online_meeting_id: str,
        organisation_id: UUID,
        integration_id: UUID,
        provider_config_key: str = "microsoft-teams",
    ) -> tuple[CICall, list[CICallTranscriptSegment], list[CICallSpeaker]] | None:
        """
        Fetch transcript for a meeting and return CICall structure.

        Returns None if no transcript is found.
        """
        # 1. List transcripts for the meeting
        # GET /me/onlineMeetings/{id}/transcripts
        endpoint = f"/v1.0/me/onlineMeetings/{online_meeting_id}/transcripts"
        try:
            resp = await self.nango.proxy_request(
                provider_config_key=provider_config_key,
                connection_id=connection_id,
                endpoint=endpoint,
            )
        except Exception:
            # If 404 or other error, assume no transcript available yet
            return None

        transcripts = resp.get("value", [])
        if not transcripts:
            return None

        # Pick the first transcript (usually there's only one, or we pick the latest?)
        # For now, let's take the first one.
        transcript_meta = transcripts[0]
        transcript_id = transcript_meta["id"]
        created_at_str = transcript_meta.get("createdDateTime")
        created_at = (
            datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at_str
            else None
        )

        # 2. Fetch metadata content (Segments with speakers)
        # GET /me/onlineMeetings/{id}/transcripts/{id}/metadataContent
        metadata_endpoint = f"{endpoint}/{transcript_id}/metadataContent"
        metadata_resp = await self.nango.proxy_request(
            provider_config_key=provider_config_key,
            connection_id=connection_id,
            endpoint=metadata_endpoint,
        )

        # Parse metadata into segments
        entries = metadata_resp.get("entries", [])

        # Create CICall object
        ci_call = CICall(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=online_meeting_id,  # Use meeting ID as external ID for the call
            title=f"Teams Meeting {online_meeting_id}",
            call_medium=CICallMedium.MEETING,
            recording_status=CICallRecordingStatus.READY,
            started_at=created_at,
            source_created_at=created_at,
            call_metadata=transcript_meta,
            transcript_language=transcript_meta.get("language"),
        )

        segments: list[CICallTranscriptSegment] = []
        speakers_map: dict[str, CICallSpeaker] = {}
        speakers_list: list[CICallSpeaker] = []

        for idx, entry in enumerate(entries):
            text_content = entry.get("text", "")
            if not text_content:
                continue

            speaker_info = entry.get("speaker", {})
            speaker_id = speaker_info.get("id", "unknown")
            speaker_name = speaker_info.get("displayName", "Unknown Speaker")

            # Create/Get Speaker
            if speaker_id not in speakers_map:
                speaker = CICallSpeaker(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    call_id=ci_call.call_id,  # Will be linked
                    external_participant_id=speaker_id,
                    speaker_name=speaker_name,
                    speaker_role=CISpeakerRole.UNKNOWN,
                )
                speakers_map[speaker_id] = speaker
                speakers_list.append(speaker)

            speaker_obj = speakers_map[speaker_id]

            segment = CICallTranscriptSegment(
                organisation_id=organisation_id,
                integration_id=integration_id,
                call_id=ci_call.call_id,
                speaker_id=speaker_obj.speaker_id,  # Will be linked
                speaker_external_id=speaker_id,
                segment_index=idx,
                content=text_content,
                content_format=TranscriptContentFormat.TEXT,
                language_code=entry.get("language"),
            )
            segments.append(segment)

        return ci_call, segments, speakers_list
