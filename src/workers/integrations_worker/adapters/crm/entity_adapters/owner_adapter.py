"""Owner entity adapter for CRM sync."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.crm.entities import OwnerSyncService


async def sync_owners(
    *,
    service: OwnerSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync CRM owner data.

    Args:
        service: OwnerSyncService instance
        integration: KnowledgeIntegration record
        records: Iterable of raw owner records from provider
        provider: Provider name (e.g., 'hubspot')

    Returns:
        Dictionary with sync results
    """
    return await service.sync(
        integration=integration,
        records=records,
        provider=provider,
    )
