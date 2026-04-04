"""Owner entity adapter for CRM sync."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.entities import OwnerSyncService

logger = get_logger(__name__)


async def sync_owners(
    *,
    service: OwnerSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync CRM owner data.

    Args:
        service: OwnerSyncService instance
        integration: KnowledgeIntegration record
        records: Iterable of raw owner records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Dictionary with sync results
    """
    # Route to incremental sync if sync_type is INCREMENTAL
    if sync_type and sync_type.upper() == "INCREMENTAL":
        result = await service.sync_incremental(
            integration=integration,
            records=records,
            provider=provider,
        )
        logger.info(
            "Owners incremental sync completed: %d processed, %d skipped",
            result.get("processed", 0),
            result.get("skipped", 0),
        )
    else:
        result = await service.sync(
            integration=integration,
            records=records,
            provider=provider,
        )
        logger.info(
            "Owners sync completed: %d processed",
            result.get("processed", 0),
        )

    return result
