"""Adapter for syncing conversation intelligence data."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.conversation_intelligence import (
    CICallAnalyticsSyncService,
    CICallSpeakerSyncService,
    CICallSyncService,
    CICallTranscriptSegmentSyncService,
)

_DATA_TYPE_MAP = {
    "calls": CICallSyncService,
    "call_speakers": CICallSpeakerSyncService,
    "speakers": CICallSpeakerSyncService,
    "transcript_segments": CICallTranscriptSegmentSyncService,
    "call_transcript_segments": CICallTranscriptSegmentSyncService,
    "segments": CICallTranscriptSegmentSyncService,
    "call_analytics": CICallAnalyticsSyncService,
    "analytics": CICallAnalyticsSyncService,
}


def _normalize_data_type(data_type: str) -> str:
    return data_type.strip().lower()


async def sync_conversation_intelligence_data(
    *,
    session: AsyncSession,
    integration: KnowledgeIntegration,
    data_type: str,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Dispatch Gong sync payloads to the appropriate service."""

    normalized = _normalize_data_type(data_type)
    service_cls = _DATA_TYPE_MAP.get(normalized)
    if service_cls is None:
        return {"status": "ignored", "reason": f"unsupported_data_type:{data_type}"}

    payloads = [record for record in records if isinstance(record, dict)]
    service = service_cls(db=session, organisation_id=integration.organisation_id)
    return await service.sync(
        integration=integration,
        records=payloads,
        provider=provider,
    )
