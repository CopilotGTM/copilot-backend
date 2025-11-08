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
) -> dict[str, Any]:
    """Sync deal task records using DealTaskSyncService.

    Args:
        db: Database session
        organisation_id: Organisation UUID string
        integration: KnowledgeIntegration instance
        records: Raw task records from provider
        provider: Provider name (e.g., 'hubspot')

    Returns:
        Sync statistics for deal tasks
    """
    service = DealTaskSyncService(db=db, organisation_id=UUID(organisation_id))
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
