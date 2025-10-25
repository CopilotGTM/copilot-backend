"""User entity adapter for CRM integrations."""

from __future__ import annotations

from typing import Any

from src.service.integrations.crm.entities.user_sync_service import UserSyncService


async def sync_users(
    service: UserSyncService,
    integration: Any,
    records: list[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync user records using UserSyncService.

    Args:
        service: UserSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw user records from provider
        provider: Provider name (e.g., 'hubspot')

    Returns:
        Dictionary with sync statistics
    """
    return await service.sync(
        integration=integration,
        records=records,
        provider=provider,
    )
