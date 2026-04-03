from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.calendar_sync_service import CalendarSyncService


async def upsert_calendar(
    *,
    session: AsyncSession,
    organisation_id: UUID,
    integration: KnowledgeIntegration,
    provider: str,
    events: Iterable[dict[str, Any]],
) -> int:
    """
    Sync calendar events from external provider into calendar_events table.

    Returns:
        Number of successfully processed records
    """
    service = CalendarSyncService(session, organisation_id)
    result = await service.sync_calendar_events(
        integration=integration, events=events, provider=provider
    )
    return result.get("records_processed", 0)


async def upsert_calendar_incremental(
    *,
    session: AsyncSession,
    organisation_id: UUID,
    integration: KnowledgeIntegration,
    provider: str,
    events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """
    Sync calendar events using incremental optimization.

    This function routes to the incremental sync method which:
    - Skips unchanged events based on change detection
    - Handles cancelled events via status field
    - Tracks action counts for observability

    Returns:
        Full sync result dict with breakdown counts.
    """
    service = CalendarSyncService(session, organisation_id)
    return await service.sync_calendar_events_incremental(
        integration=integration, events=events, provider=provider
    )
