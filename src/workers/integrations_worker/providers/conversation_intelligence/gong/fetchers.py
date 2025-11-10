"""Fetch Gong conversation intelligence data via Nango actions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.core import BaseFetcher


class GongActionFetcher(BaseFetcher):
    """Base fetcher that triggers provider-defined Nango actions."""

    action_name: str
    data_type: str

    def __init__(
        self,
        nango_client: NangoClient,
        provider_config_key: str,
        connection_id: str,
        *,
        action_name: str,
        data_type: str,
    ) -> None:
        super().__init__(nango_client, provider_config_key, connection_id)
        self.action_name = action_name
        self.data_type = data_type

    async def fetch_all(
        self,
        *,
        limit: int = 500,
        incremental: bool = False,
        last_synced_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "limit": limit,
            "data_type": self.data_type,
        }
        if incremental and last_synced_at:
            payload["since"] = last_synced_at.astimezone(UTC).isoformat()

        response = await self.nango_client.action(
            provider_config_key=self.provider_config_key,
            connection_id=self.connection_id,
            action_name=self.action_name,
            input_data=payload,
        )
        records = self._extract_records(response)
        return [self._normalize(record) for record in records if isinstance(record, dict)]

    def _extract_records(self, response: Any) -> list[dict[str, Any]]:
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]

        if not isinstance(response, dict):
            return []

        for key in ("records", "results", "items", "data"):
            value = response.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = self._extract_records(value)
                if nested:
                    return nested
        return []

    def _normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        return record


class GongCallsFetcher(GongActionFetcher):
    """Normalize Gong call metadata."""

    def __init__(self, nango: NangoClient, provider_config_key: str, connection_id: str) -> None:
        super().__init__(
            nango,
            provider_config_key,
            connection_id,
            action_name="gong-fetch-calls",
            data_type="calls",
        )

    def _normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        external_id = _first_non_empty(record, "external_id", "call_id", "id", "conversation_id")
        if not external_id:
            return {}

        return {
            "external_id": external_id,
            "title": record.get("title") or record.get("subject"),
            "description": record.get("description"),
            "call_medium": (
                record.get("call_medium") or record.get("meeting_type") or "meeting"
            ).lower(),
            "recording_status": (
                record.get("recording_status") or record.get("status") or "processing"
            ).lower(),
            "started_at": record.get("started_at") or record.get("start_time"),
            "ended_at": record.get("ended_at") or record.get("end_time"),
            "duration_seconds": record.get("duration_seconds") or record.get("duration"),
            "timezone": record.get("timezone"),
            "meeting_url": record.get("meeting_url") or record.get("web_join_url"),
            "recording_url": record.get("recording_url") or record.get("recording_link"),
            "transcript_language": record.get("transcript_language"),
            "owner_user_external_id": record.get("owner_user_external_id")
            or record.get("owner_id"),
            "primary_deal_external_id": record.get("primary_deal_external_id")
            or record.get("deal_id"),
            "source_created_at": record.get("created_at") or record.get("source_created_at"),
            "source_modified_at": record.get("updated_at") or record.get("source_modified_at"),
            "call_metadata": record.get("metadata") or {},
            "data_payload": record,
        }


class GongSpeakersFetcher(GongActionFetcher):
    """Normalize Gong participant metadata."""

    def __init__(self, nango: NangoClient, provider_config_key: str, connection_id: str) -> None:
        super().__init__(
            nango,
            provider_config_key,
            connection_id,
            action_name="gong-fetch-speakers",
            data_type="call_speakers",
        )

    def _normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        call_id = _first_non_empty(record, "call_external_id", "call_id", "conversation_id")
        participant_id = _first_non_empty(
            record,
            "external_participant_id",
            "participant_id",
            "speaker_external_id",
        )
        if not call_id or not participant_id:
            return {}

        return {
            "call_external_id": call_id,
            "external_participant_id": participant_id,
            "speaker_role": record.get("speaker_role") or record.get("role"),
            "speaker_name": record.get("speaker_name") or record.get("name"),
            "speaker_email": record.get("speaker_email") or record.get("email"),
            "crm_contact_id": record.get("crm_contact_id"),
            "crm_user_id": record.get("crm_user_id"),
            "talk_time_seconds": record.get("talk_time_seconds") or record.get("talk_time"),
            "talk_ratio": record.get("talk_ratio"),
            "sentiment_score": record.get("sentiment_score"),
            "speaker_metadata": record.get("speaker_metadata") or record.get("metadata") or {},
            "source_modified_at": record.get("updated_at") or record.get("source_modified_at"),
            "data_payload": record,
        }


class GongTranscriptFetcher(GongActionFetcher):
    """Normalize Gong transcript segments."""

    def __init__(self, nango: NangoClient, provider_config_key: str, connection_id: str) -> None:
        super().__init__(
            nango,
            provider_config_key,
            connection_id,
            action_name="gong-fetch-transcript-segments",
            data_type="transcript_segments",
        )

    def _normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        call_id = _first_non_empty(record, "call_external_id", "call_id", "conversation_id")
        if not call_id:
            return {}
        segment_index = record.get("segment_index")
        if segment_index is None:
            segment_index = record.get("index")
        if segment_index is None:
            return {}

        return {
            "call_external_id": call_id,
            "segment_index": segment_index,
            "speaker_external_id": _first_non_empty(
                record,
                "speaker_external_id",
                "participant_id",
                "external_participant_id",
            ),
            "start_time_seconds": record.get("start_time_seconds") or record.get("start"),
            "end_time_seconds": record.get("end_time_seconds") or record.get("end"),
            "content": record.get("content") or record.get("text") or "",
            "content_format": record.get("content_format") or "text",
            "language_code": record.get("language_code") or record.get("language"),
            "sentiment_score": record.get("sentiment_score"),
            "keywords": record.get("keywords") or record.get("trackers") or [],
            "data_payload": record,
            "source_modified_at": record.get("updated_at") or record.get("source_modified_at"),
        }


class GongAnalyticsFetcher(GongActionFetcher):
    """Normalize Gong analytics snapshots."""

    def __init__(self, nango: NangoClient, provider_config_key: str, connection_id: str) -> None:
        super().__init__(
            nango,
            provider_config_key,
            connection_id,
            action_name="gong-fetch-analytics",
            data_type="call_analytics",
        )

    def _normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        call_id = _first_non_empty(record, "call_external_id", "call_id", "conversation_id")
        if not call_id:
            return {}

        return {
            "call_external_id": call_id,
            "analysis_type": record.get("analysis_type") or record.get("type") or "summary",
            "generated_at": record.get("generated_at") or record.get("created_at"),
            "summary": record.get("summary"),
            "highlights": record.get("highlights") or [],
            "action_items": record.get("action_items") or record.get("actions") or [],
            "topics": record.get("topics") or record.get("trackers") or [],
            "overall_sentiment": record.get("overall_sentiment") or record.get("sentiment"),
            "talk_time_ratio": record.get("talk_time_ratio"),
            "analytics_payload": record.get("analytics_payload") or record.get("metrics") or {},
            "source_modified_at": record.get("updated_at") or record.get("source_modified_at"),
            "data_payload": record,
        }


def _first_non_empty(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def fetch_calls(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None,
    provider_config_key: str,
) -> list[dict[str, Any]]:
    _ = data_type
    fetcher = GongCallsFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial, last_synced_at=last_synced_at)


async def fetch_speakers(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None,
    provider_config_key: str,
) -> list[dict[str, Any]]:
    _ = data_type
    fetcher = GongSpeakersFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial, last_synced_at=last_synced_at)


async def fetch_transcript_segments(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None,
    provider_config_key: str,
) -> list[dict[str, Any]]:
    _ = data_type
    fetcher = GongTranscriptFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial, last_synced_at=last_synced_at)


async def fetch_analytics(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None,
    provider_config_key: str,
) -> list[dict[str, Any]]:
    _ = data_type
    fetcher = GongAnalyticsFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial, last_synced_at=last_synced_at)


__all__ = [
    "GongCallsFetcher",
    "GongSpeakersFetcher",
    "GongTranscriptFetcher",
    "GongAnalyticsFetcher",
    "fetch_calls",
    "fetch_speakers",
    "fetch_transcript_segments",
    "fetch_analytics",
]
