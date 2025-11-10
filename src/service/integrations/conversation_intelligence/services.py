"""Sync services for conversation intelligence data."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.conversation_intelligence import (
    CICall,
    CICallAnalyticsSnapshot,
    CICallMedium,
    CICallRecordingStatus,
    CICallSpeaker,
    CICallTranscriptSegment,
    CISpeakerRole,
    TranscriptContentFormat,
)
from src.domain.models.organisation_tables.sync.crm import CRMDeal
from src.service.integrations.core import BaseSyncService

Payload = dict[str, Any]


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _parse_uuid(value: Any) -> UUID | None:
    if not value:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        try:
            return UUID(trimmed)
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _ensure_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    if isinstance(value, set | tuple):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _to_enum(enum_cls, value: Any, default):
    if value is None:
        return default
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        for option in enum_cls:
            if option.value == lowered:
                return option
        try:
            return enum_cls(lowered)
        except ValueError:
            return default
    return default


class CICallSyncService(BaseSyncService[CICall]):
    """Sync Gong/Chorus call metadata."""

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[Payload],
        _provider: str,
    ) -> dict[str, Any]:
        result = self._init_result()
        payloads = [payload for payload in records if isinstance(payload, dict)]
        if not payloads:
            result.complete()
            return {"calls": 0, **result.to_dict()}

        external_ids: list[str] = []
        deal_external_ids: set[str] = set()
        normalized: list[Payload] = []

        for payload in payloads:
            external_id = _coerce_call_external_id(payload)
            if not external_id:
                result.increment_failed()
                result.add_error("Call payload missing external_id")
                continue
            payload = dict(payload)
            payload["external_id"] = external_id
            external_ids.append(external_id)
            if payload.get("primary_deal_external_id"):
                deal_external_ids.add(str(payload["primary_deal_external_id"]))
            normalized.append(payload)

        existing = await self._load_existing_by_external_ids(
            CICall,
            external_ids,
            integration.integration_id,
        )
        deal_map = await self._resolve_deal_ids(deal_external_ids)
        now = utcnow()
        synced: dict[str, CICall] = {}

        for payload in normalized:
            external_id = payload["external_id"]
            record = existing.get(external_id)
            is_new = record is None
            if record is None:
                record = CICall(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    external_id=external_id,
                )
            _apply_call_payload(record, payload, deal_map)
            record.synced_at = now
            record.last_seen_at = now
            self.db.add(record)
            synced[external_id] = record
            result.increment_processed()
            if is_new:
                result.increment_created()
            else:
                result.increment_updated()

        await self.db.commit()
        for record in synced.values():
            await self.db.refresh(record)

        result.complete()
        return {
            "calls": len(synced),
            **result.to_dict(),
        }

    async def _resolve_deal_ids(self, external_ids: set[str]) -> dict[str, UUID]:
        if not external_ids:
            return {}
        stmt = (
            select(CRMDeal.external_id, CRMDeal.deal_id)
            .where(CRMDeal.organisation_id == self.organisation_id)
            .where(CRMDeal.external_id.in_(list(external_ids)))
        )
        rows = (await self.db.execute(stmt)).all()
        return {row.external_id: row.deal_id for row in rows}

    def _transform_record(self, raw: dict[str, Any], provider: str) -> CICall:  # noqa: ARG002
        return CICall(
            organisation_id=self.organisation_id, integration_id=UUID(int=0), external_id=""
        )


class CICallSpeakerSyncService(BaseSyncService[CICallSpeaker]):
    """Sync call speaker metadata."""

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[Payload],
        _provider: str,
    ) -> dict[str, Any]:
        result = self._init_result()
        payloads = [payload for payload in records if isinstance(payload, dict)]
        if not payloads:
            result.complete()
            return {"speakers": 0, **result.to_dict()}

        call_external_ids = {
            payload.get("call_external_id")
            for payload in payloads
            if payload.get("call_external_id")
        }
        call_map = await self._load_calls(integration.integration_id, call_external_ids)
        if not call_map:
            result.complete()
            return {"speakers": 0, **result.to_dict()}

        existing = await self._load_existing_speakers(integration.integration_id, call_map.values())
        now = utcnow()
        processed = 0

        for payload in payloads:
            call_external_id = payload.get("call_external_id")
            if not call_external_id:
                result.increment_failed()
                result.add_error("Speaker missing call_external_id")
                continue

            call = call_map.get(call_external_id)
            if not call:
                result.increment_failed()
                result.add_error(f"Unknown call reference for speaker: {call_external_id}")
                continue

            external_key = _resolve_speaker_key(payload)
            if not external_key:
                result.increment_failed()
                result.add_error("Speaker payload missing external identifier")
                continue

            speaker = existing.get((call.call_id, external_key))
            is_new = speaker is None
            if speaker is None:
                speaker = CICallSpeaker(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    call_id=call.call_id,
                    external_participant_id=external_key,
                )

            _apply_speaker_payload(speaker, payload)
            speaker.synced_at = now
            speaker.last_seen_at = now
            self.db.add(speaker)
            existing[(call.call_id, external_key)] = speaker
            processed += 1
            result.increment_processed()
            if is_new:
                result.increment_created()
            else:
                result.increment_updated()

        await self.db.commit()
        result.complete()
        return {
            "speakers": processed,
            **result.to_dict(),
        }

    async def _load_calls(
        self,
        integration_id: UUID,
        external_ids: set[str | None],
    ) -> dict[str, CICall]:
        ids = [ext for ext in external_ids if ext]
        if not ids:
            return {}
        stmt = (
            select(CICall)
            .where(CICall.integration_id == integration_id)
            .where(CICall.external_id.in_(ids))
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_existing_speakers(
        self,
        integration_id: UUID,
        calls: Iterable[CICall],
    ) -> dict[tuple[UUID, str], CICallSpeaker]:
        call_ids = [call.call_id for call in calls]
        if not call_ids:
            return {}
        stmt = (
            select(CICallSpeaker)
            .where(CICallSpeaker.integration_id == integration_id)
            .where(CICallSpeaker.call_id.in_(call_ids))
        )
        result = await self.db.execute(stmt)
        speakers = {}
        for speaker in result.scalars().all():
            key = speaker.external_participant_id or speaker.speaker_email or speaker.speaker_name
            if key:
                speakers[(speaker.call_id, key)] = speaker
        return speakers

    def _transform_record(self, raw: dict[str, Any], provider: str) -> CICallSpeaker:  # noqa: ARG002
        return CICallSpeaker(
            organisation_id=self.organisation_id, integration_id=UUID(int=0), call_id=UUID(int=0)
        )


class CICallTranscriptSegmentSyncService(BaseSyncService[CICallTranscriptSegment]):
    """Sync transcript segments."""

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[Payload],
        _provider: str,
    ) -> dict[str, Any]:
        result = self._init_result()
        payloads = [payload for payload in records if isinstance(payload, dict)]
        if not payloads:
            result.complete()
            return {"segments": 0, **result.to_dict()}

        call_external_ids = {
            payload.get("call_external_id")
            for payload in payloads
            if payload.get("call_external_id")
        }
        call_map = await self._load_calls(integration.integration_id, call_external_ids)
        if not call_map:
            result.complete()
            return {"segments": 0, **result.to_dict()}

        speaker_map = await self._load_speaker_map(integration.integration_id, call_map.values())

        existing = await self._load_existing_segments(
            integration.integration_id, call_map, payloads
        )
        now = utcnow()
        processed = 0

        for payload in payloads:
            call_external_id = payload.get("call_external_id")
            if not call_external_id:
                result.increment_failed()
                result.add_error("Segment missing call_external_id")
                continue

            call = call_map.get(call_external_id)
            if not call:
                result.increment_failed()
                result.add_error(f"Unknown call reference for segment: {call_external_id}")
                continue

            segment_index = _to_int(payload.get("segment_index"))
            if segment_index is None:
                result.increment_failed()
                result.add_error("Segment missing segment_index")
                continue

            segment = existing.get((call.call_id, segment_index))
            is_new = segment is None
            if segment is None:
                segment = CICallTranscriptSegment(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    call_id=call.call_id,
                    segment_index=segment_index,
                )

            speaker_external_id = payload.get("speaker_external_id")
            speaker_id = None
            if speaker_external_id:
                speaker_id = speaker_map.get((call.call_id, speaker_external_id))
            _apply_segment_payload(segment, payload, speaker_id)
            segment.synced_at = now
            segment.last_seen_at = now
            self.db.add(segment)
            existing[(call.call_id, segment_index)] = segment
            processed += 1
            result.increment_processed()
            if is_new:
                result.increment_created()
            else:
                result.increment_updated()

        await self.db.commit()
        result.complete()
        return {
            "segments": processed,
            **result.to_dict(),
        }

    async def _load_calls(
        self,
        integration_id: UUID,
        external_ids: set[str | None],
    ) -> dict[str, CICall]:
        ids = [ext for ext in external_ids if ext]
        if not ids:
            return {}
        stmt = (
            select(CICall)
            .where(CICall.integration_id == integration_id)
            .where(CICall.external_id.in_(ids))
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_speaker_map(
        self,
        integration_id: UUID,
        calls: Iterable[CICall],
    ) -> dict[tuple[UUID, str], UUID]:
        call_ids = [call.call_id for call in calls]
        if not call_ids:
            return {}
        stmt = (
            select(CICallSpeaker)
            .where(CICallSpeaker.integration_id == integration_id)
            .where(CICallSpeaker.call_id.in_(call_ids))
        )
        result = await self.db.execute(stmt)
        mapping: dict[tuple[UUID, str], UUID] = {}
        for speaker in result.scalars().all():
            key = speaker.external_participant_id or speaker.speaker_email or speaker.speaker_name
            if key:
                mapping[(speaker.call_id, key)] = speaker.speaker_id
        return mapping

    async def _load_existing_segments(
        self,
        integration_id: UUID,
        call_map: dict[str, CICall],
        payloads: list[Payload],
    ) -> dict[tuple[UUID, int], CICallTranscriptSegment]:
        indices_by_call: dict[UUID, set[int]] = defaultdict(set)
        for payload in payloads:
            call_external_id = payload.get("call_external_id")
            segment_index = _to_int(payload.get("segment_index"))
            call = call_map.get(call_external_id) if call_external_id else None
            if call and segment_index is not None:
                indices_by_call[call.call_id].add(segment_index)

        if not indices_by_call:
            return {}

        stmt = (
            select(CICallTranscriptSegment)
            .where(CICallTranscriptSegment.integration_id == integration_id)
            .where(CICallTranscriptSegment.call_id.in_(list(indices_by_call.keys())))
        )
        result = await self.db.execute(stmt)
        segments: dict[tuple[UUID, int], CICallTranscriptSegment] = {}
        for segment in result.scalars().all():
            if segment.segment_index in indices_by_call.get(segment.call_id, set()):
                segments[(segment.call_id, segment.segment_index)] = segment
        return segments

    def _transform_record(self, raw: dict[str, Any], provider: str) -> CICallTranscriptSegment:  # noqa: ARG002
        return CICallTranscriptSegment(
            organisation_id=self.organisation_id,
            integration_id=UUID(int=0),
            call_id=UUID(int=0),
            segment_index=0,
            content="",
        )


class CICallAnalyticsSyncService(BaseSyncService[CICallAnalyticsSnapshot]):
    """Sync analytics snapshots."""

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[Payload],
        _provider: str,
    ) -> dict[str, Any]:
        result = self._init_result()
        payloads = [payload for payload in records if isinstance(payload, dict)]
        if not payloads:
            result.complete()
            return {"analytics": 0, **result.to_dict()}

        call_external_ids = {
            payload.get("call_external_id")
            for payload in payloads
            if payload.get("call_external_id")
        }
        call_map = await self._load_calls(integration.integration_id, call_external_ids)
        if not call_map:
            result.complete()
            return {"analytics": 0, **result.to_dict()}

        existing = await self._load_existing_snapshots(
            integration.integration_id, call_map.values()
        )
        now = utcnow()
        processed = 0

        for payload in payloads:
            call_external_id = payload.get("call_external_id")
            if not call_external_id:
                result.increment_failed()
                result.add_error("Analytics missing call_external_id")
                continue

            call = call_map.get(call_external_id)
            if not call:
                result.increment_failed()
                result.add_error(f"Unknown call reference for analytics: {call_external_id}")
                continue

            analysis_type = str(payload.get("analysis_type") or "summary").strip() or "summary"
            snapshot = existing.get((call.call_id, analysis_type))
            is_new = snapshot is None
            if snapshot is None:
                snapshot = CICallAnalyticsSnapshot(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    call_id=call.call_id,
                    analysis_type=analysis_type,
                )

            _apply_analytics_payload(snapshot, payload)
            snapshot.synced_at = now
            snapshot.last_seen_at = now
            self.db.add(snapshot)
            existing[(call.call_id, analysis_type)] = snapshot
            processed += 1
            result.increment_processed()
            if is_new:
                result.increment_created()
            else:
                result.increment_updated()

        await self.db.commit()
        result.complete()
        return {
            "analytics": processed,
            **result.to_dict(),
        }

    async def _load_calls(
        self,
        integration_id: UUID,
        external_ids: set[str | None],
    ) -> dict[str, CICall]:
        ids = [ext for ext in external_ids if ext]
        if not ids:
            return {}
        stmt = (
            select(CICall)
            .where(CICall.integration_id == integration_id)
            .where(CICall.external_id.in_(ids))
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_existing_snapshots(
        self,
        integration_id: UUID,
        calls: Iterable[CICall],
    ) -> dict[tuple[UUID, str], CICallAnalyticsSnapshot]:
        call_ids = [call.call_id for call in calls]
        if not call_ids:
            return {}
        stmt = (
            select(CICallAnalyticsSnapshot)
            .where(CICallAnalyticsSnapshot.integration_id == integration_id)
            .where(CICallAnalyticsSnapshot.call_id.in_(call_ids))
        )
        result = await self.db.execute(stmt)
        snapshots = {}
        for snapshot in result.scalars().all():
            snapshots[(snapshot.call_id, snapshot.analysis_type)] = snapshot
        return snapshots

    def _transform_record(self, raw: dict[str, Any], provider: str) -> CICallAnalyticsSnapshot:  # noqa: ARG002
        return CICallAnalyticsSnapshot(
            organisation_id=self.organisation_id,
            integration_id=UUID(int=0),
            call_id=UUID(int=0),
            analysis_type="summary",
        )


def _coerce_call_external_id(payload: Payload) -> str | None:
    for key in ("external_id", "call_id", "id", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _apply_call_payload(
    record: CICall,
    payload: Payload,
    deal_map: dict[str, UUID],
) -> None:
    record.title = payload.get("title")
    record.description = payload.get("description")
    record.call_medium = _to_enum(CICallMedium, payload.get("call_medium"), record.call_medium)
    record.recording_status = _to_enum(
        CICallRecordingStatus,
        payload.get("recording_status"),
        record.recording_status,
    )
    record.started_at = _parse_datetime(payload.get("started_at"))
    record.ended_at = _parse_datetime(payload.get("ended_at"))
    record.duration_seconds = _to_int(payload.get("duration_seconds"))
    record.timezone = payload.get("timezone")
    record.meeting_url = payload.get("meeting_url")
    record.recording_url = payload.get("recording_url")
    record.transcript_language = payload.get("transcript_language")
    record.owner_user_external_id = payload.get("owner_user_external_id")
    record.crm_owner_id = _parse_uuid(payload.get("crm_owner_id")) or record.crm_owner_id

    deal_external_id = payload.get("primary_deal_external_id")
    record.primary_deal_external_id = deal_external_id or record.primary_deal_external_id
    if isinstance(deal_external_id, str) and deal_external_id in deal_map:
        record.primary_deal_id = deal_map[deal_external_id]
    elif deal_external_id is None:
        record.primary_deal_id = None

    record.source_created_at = _parse_datetime(payload.get("source_created_at"))
    record.source_modified_at = _parse_datetime(payload.get("source_modified_at"))
    record.call_metadata = _ensure_dict(payload.get("call_metadata"))
    record.data_payload = payload.get("data_payload") or payload


def _resolve_speaker_key(payload: Payload) -> str | None:
    for key in (
        "external_participant_id",
        "speaker_external_id",
        "participant_id",
        "speaker_email",
        "speaker_name",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _apply_speaker_payload(record: CICallSpeaker, payload: Payload) -> None:
    record.external_participant_id = (
        payload.get("external_participant_id") or record.external_participant_id
    )
    record.speaker_role = _to_enum(CISpeakerRole, payload.get("speaker_role"), record.speaker_role)
    record.speaker_name = payload.get("speaker_name")
    record.speaker_email = payload.get("speaker_email")
    record.crm_contact_id = _parse_uuid(payload.get("crm_contact_id")) or record.crm_contact_id
    record.crm_user_id = _parse_uuid(payload.get("crm_user_id")) or record.crm_user_id
    record.talk_time_seconds = _to_int(payload.get("talk_time_seconds"))
    record.talk_ratio = _to_float(payload.get("talk_ratio"))
    record.sentiment_score = _to_float(payload.get("sentiment_score"))
    record.speaker_metadata = _ensure_dict(payload.get("speaker_metadata"))
    record.source_modified_at = _parse_datetime(payload.get("source_modified_at"))


def _apply_segment_payload(
    record: CICallTranscriptSegment,
    payload: Payload,
    speaker_id: UUID | None,
) -> None:
    record.speaker_id = speaker_id
    record.start_time_seconds = _to_float(payload.get("start_time_seconds"))
    record.end_time_seconds = _to_float(payload.get("end_time_seconds"))
    record.content = payload.get("content") or record.content
    record.content_format = _to_enum(
        TranscriptContentFormat,
        payload.get("content_format"),
        record.content_format,
    )
    record.language_code = payload.get("language_code")
    record.sentiment_score = _to_float(payload.get("sentiment_score"))
    record.keywords = _ensure_list(payload.get("keywords"))
    record.data_payload = payload.get("data_payload") or payload
    record.source_modified_at = _parse_datetime(payload.get("source_modified_at"))
    record.speaker_external_id = payload.get("speaker_external_id") or record.speaker_external_id


def _apply_analytics_payload(record: CICallAnalyticsSnapshot, payload: Payload) -> None:
    record.generated_at = _parse_datetime(payload.get("generated_at"))
    record.summary = payload.get("summary")
    record.highlights = _ensure_list(payload.get("highlights"))
    record.action_items = _ensure_list(payload.get("action_items"))
    record.topics = _ensure_list(payload.get("topics"))
    record.overall_sentiment = _to_float(payload.get("overall_sentiment"))
    record.talk_time_ratio = _to_float(payload.get("talk_time_ratio"))
    record.analytics_payload = _ensure_dict(payload.get("analytics_payload"))
    record.source_modified_at = _parse_datetime(payload.get("source_modified_at"))
