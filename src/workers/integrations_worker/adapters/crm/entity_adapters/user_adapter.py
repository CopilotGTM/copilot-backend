"""User entity adapter for CRM integrations."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.entities.user_sync_service import UserSyncService

logger = get_logger(__name__)


async def sync_users(
    service: UserSyncService,
    integration: Any,
    records: list[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync user records using UserSyncService.

    Args:
        service: UserSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw user records from provider
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
            "Users incremental sync completed: %d processed, %d skipped",
            result.get("users", 0),
            result.get("skipped", 0),
        )
    else:
        result = await service.sync(
            integration=integration,
            records=records,
            provider=provider,
        )
        logger.info(
            "Users sync completed: %d processed",
            result.get("users", 0),
        )

    return result
