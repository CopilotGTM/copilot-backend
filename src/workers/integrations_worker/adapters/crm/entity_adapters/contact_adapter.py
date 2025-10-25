"""Contact entity adapter for CRM sync."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.crm.entities.contact_sync_service import ContactSyncService


async def sync_contacts(
    *,
    db: AsyncSession,
    organisation_id: str,
    integration: KnowledgeIntegration,
    records: list[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync contact records using ContactSyncService.

    Args:
        db: Database session
        organisation_id: Organisation UUID string
        integration: KnowledgeIntegration instance
        records: Raw contact records from provider
        provider: Provider name (e.g., 'hubspot')

    Returns:
        Sync statistics
    """
    service = ContactSyncService(db=db, organisation_id=UUID(organisation_id))
    return await service.sync(
        integration=integration,
        records=records,
        provider=provider,
    )
