"""Adapter for syncing CRM data (HubSpot deals)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.crm_sync_service import CRMSyncService


async def sync_crm_data(
    *,
    service: CRMSyncService,
    integration: KnowledgeIntegration,
    data_type: str,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Dispatch CRM data sync by data_type."""

    if data_type == "pipelines":
        processed = await service.sync_pipelines(
            integration=integration,
            pipelines=records,
            provider=provider,
        )
        return {"records_synced": processed}

    if data_type == "deals":
        return await service.sync_deals(
            integration=integration,
            deals=records,
            provider=provider,
        )

    if data_type == "contacts":
        return await service.sync_contacts(
            integration=integration,
            contacts=records,
            provider=provider,
        )

    if data_type == "users":
        return await service.sync_users(
            integration=integration,
            users=records,
            provider=provider,
        )

    if data_type == "companies":
        return await service.sync_companies(
            integration=integration,
            companies=records,
            provider=provider,
        )

    raise ValueError(f"Unsupported CRM data_type: {data_type}")
