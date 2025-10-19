from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.sync_service import SyncService


async def upsert_calendar(
    *,
    session: AsyncSession,
    organisation_id: UUID,
    integration: KnowledgeIntegration,
    provider: str,
    events: Iterable[dict[str, Any]],
) -> int:
    service = SyncService(session, organisation_id)
    return await service.upsert_calendar_events(
        integration=integration, events=events, provider=provider
    )
