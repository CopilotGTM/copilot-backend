"""Pipeline entity adapter for CRM integrations."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.entities.pipeline_sync_service import PipelineSyncService

logger = get_logger(__name__)


async def sync_pipelines(
    service: PipelineSyncService,
    integration: Any,
    records: list[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync pipeline records using PipelineSyncService.

    Args:
        service: PipelineSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw pipeline records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Dictionary with sync statistics
    """
    # Route to incremental sync if sync_type is INCREMENTAL
    if sync_type and sync_type.upper() == "INCREMENTAL":
        result = await service.sync_incremental(
            integration=integration,
            records=records,
            provider=provider,
        )
        logger.info(
            "Pipelines incremental sync completed: %d pipelines, %d stages, %d skipped",
            result.get("pipelines", 0),
            result.get("stages", 0),
            result.get("skipped", 0),
        )
    else:
        result = await service.sync(
            integration=integration,
            records=records,
            provider=provider,
        )
        logger.info(
            "Pipelines sync completed: %d pipelines, %d stages",
            result.get("pipelines", 0),
            result.get("stages", 0),
        )

    return result
