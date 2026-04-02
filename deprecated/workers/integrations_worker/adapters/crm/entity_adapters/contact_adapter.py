"""Contact entity adapter for CRM sync."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.entities.contact_sync_service import ContactSyncService

logger = get_logger(__name__)


async def sync_contacts(
    *,
    db: AsyncSession,
    organisation_id: str,
    integration: KnowledgeIntegration,
    records: list[dict[str, Any]],
    provider: str,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync contact records using ContactSyncService.

    For incremental syncs (sync_type="INCREMENTAL"), uses optimized sync method that:
    - Routes records by database state (new vs existing)
    - Skips unchanged records to reduce database writes

    Args:
        db: Database session
        organisation_id: Organisation UUID string
        integration: KnowledgeIntegration instance
        records: Raw contact records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    service = ContactSyncService(db=db, organisation_id=UUID(organisation_id))

    # Use incremental sync for INCREMENTAL sync type
    if sync_type and sync_type.upper() == "INCREMENTAL":
        logger.info(
            "Using incremental contact sync: provider=%s record_count=%d",
            provider,
            len(records),
        )
        return await service.sync_incremental(
            integration=integration,
            records=records,
            provider=provider,
        )

    # For INITIAL sync or unknown sync_type, use full sync
    logger.info(
        "Using full contact sync: provider=%s sync_type=%s record_count=%d",
        provider,
        sync_type,
        len(records),
    )
    return await service.sync(
        integration=integration,
        records=records,
        provider=provider,
    )
