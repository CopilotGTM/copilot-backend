"""Pipeline entity adapter for CRM integrations."""

from __future__ import annotations

from typing import Any

from src.service.integrations.crm.entities.pipeline_sync_service import PipelineSyncService


async def sync_pipelines(
    service: PipelineSyncService,
    integration: Any,
    records: list[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync pipeline records using PipelineSyncService.

    Args:
        service: PipelineSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw pipeline records from provider
        provider: Provider name (e.g., 'hubspot')

    Returns:
        Dictionary with sync statistics
    """
    return await service.sync(
        integration=integration,
        records=records,
        provider=provider,
    )
