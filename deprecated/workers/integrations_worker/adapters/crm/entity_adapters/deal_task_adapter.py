"""Deal task entity adapter for CRM sync."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.entities.deal_task_sync_service import (
    DealTaskSyncService,
)

logger = get_logger(__name__)


async def sync_deal_tasks(
    *,
    db: AsyncSession,
    organisation_id: str,
    integration: KnowledgeIntegration,
    records: list[dict[str, Any]],
    provider: str,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync deal task records using DealTaskSyncService.

    Args:
        db: Database session
        organisation_id: Organisation UUID string
        integration: KnowledgeIntegration instance
        records: Raw task records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics for deal tasks
    """
    service = DealTaskSyncService(db=db, organisation_id=UUID(organisation_id))

    # Route to incremental sync if sync_type is INCREMENTAL
    if sync_type and sync_type.upper() == "INCREMENTAL":
        result = await service.sync_incremental(
            integration=integration,
            records=records,
            provider=provider,
        )
        logger.info(
            "Deal tasks incremental sync completed: %d tasks processed, %d skipped",
            result.get("deal_tasks", 0),
            result.get("skipped", 0),
        )
    else:
        result = await service.sync(
            integration=integration,
            records=records,
            provider=provider,
        )
        logger.info(
            "Deal tasks sync completed: %d tasks processed",
            result.get("deal_tasks", 0),
        )

    return result
