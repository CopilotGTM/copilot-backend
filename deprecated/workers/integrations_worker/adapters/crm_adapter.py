"""Adapter for syncing CRM data (HubSpot deals)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.service.integrations.crm.entities import (
    CompanySyncService,
    ContactSyncService,
    DealSyncService,
    DealTaskSyncService,
    OwnerSyncService,
    PipelineSyncService,
    UserSyncService,
)
from src.service.integrations.crm_sync_service import CRMSyncService
from src.workers.integrations_worker.adapters.crm.entity_adapters import (
    sync_companies,
    sync_contacts,
    sync_deal_tasks,
    sync_deals,
    sync_owners,
    sync_pipelines,
    sync_users,
)


async def _sync_owners(
    service: OwnerSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync owners using new entity pattern.

    Args:
        service: OwnerSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw owner records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    return await sync_owners(
        service=service,
        integration=integration,
        records=records,
        provider=provider,
        sync_type=sync_type,
    )


async def _sync_deals(
    service: DealSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync deals using new entity pattern.

    Args:
        service: DealSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw deal records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    return await sync_deals(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
        sync_type=sync_type,
    )


async def _sync_contacts(
    service: ContactSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync contacts using new entity pattern.

    Args:
        service: ContactSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw contact records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    return await sync_contacts(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
        sync_type=sync_type,
    )


async def _sync_companies(
    service: CompanySyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync companies using new entity pattern.

    Args:
        service: CompanySyncService instance
        integration: KnowledgeIntegration instance
        records: Raw company records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    return await sync_companies(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
        sync_type=sync_type,
    )


async def _sync_users(
    service: UserSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync users using new entity pattern.

    Args:
        service: UserSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw user records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    return await sync_users(
        service=service,
        integration=integration,
        records=list(records),
        provider=provider,
        sync_type=sync_type,
    )


async def _sync_pipelines(
    service: PipelineSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync pipelines using new entity pattern.

    Args:
        service: PipelineSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw pipeline records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    return await sync_pipelines(
        service=service,
        integration=integration,
        records=list(records),
        provider=provider,
        sync_type=sync_type,
    )


async def _sync_deal_tasks(
    service: DealTaskSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Sync deal tasks using new entity pattern.

    Args:
        service: DealTaskSyncService instance
        integration: KnowledgeIntegration instance
        records: Raw task records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics
    """
    return await sync_deal_tasks(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
        sync_type=sync_type,
    )


async def sync_crm_data(
    *,
    service: (
        CRMSyncService
        | OwnerSyncService
        | DealSyncService
        | DealTaskSyncService
        | ContactSyncService
        | CompanySyncService
        | UserSyncService
        | PipelineSyncService
    ),
    integration: KnowledgeIntegration,
    data_type: str,
    records: Iterable[dict[str, Any]],
    provider: str,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Dispatch CRM data sync by data_type.

    Args:
        service: Appropriate sync service for the data type
        integration: KnowledgeIntegration instance
        data_type: Type of data being synced (deals, contacts, etc.)
        records: Raw records from provider
        provider: Provider name (e.g., 'hubspot')
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)

    Returns:
        Sync statistics dictionary
    """

    # Handle entities that support incremental sync with sync_type
    # Deals
    if data_type in ("deals", "opportunities") and isinstance(service, DealSyncService):
        return await _sync_deals(service, integration, records, provider, sync_type=sync_type)

    # Contacts
    if data_type in ("contacts", "leads") and isinstance(service, ContactSyncService):
        return await _sync_contacts(service, integration, records, provider, sync_type=sync_type)

    # Companies
    if data_type in ("companies", "accounts") and isinstance(service, CompanySyncService):
        return await _sync_companies(service, integration, records, provider, sync_type=sync_type)

    # Deal Tasks (includes tasks, tickets, notes aliases)
    if data_type in ("deal_tasks", "tasks", "tickets", "notes") and isinstance(
        service, DealTaskSyncService
    ):
        return await _sync_deal_tasks(service, integration, records, provider, sync_type=sync_type)

    # Owners
    if data_type == "owners" and isinstance(service, OwnerSyncService):
        return await _sync_owners(service, integration, records, provider, sync_type=sync_type)

    # Users
    if data_type == "users" and isinstance(service, UserSyncService):
        return await _sync_users(service, integration, records, provider, sync_type=sync_type)

    # Pipelines
    if data_type == "pipelines" and isinstance(service, PipelineSyncService):
        return await _sync_pipelines(service, integration, records, provider, sync_type=sync_type)

    # Fall back to legacy CRMSyncService (should never reach here)
    if isinstance(service, CRMSyncService):
        error_msg = (
            f"Legacy CRMSyncService path reached for '{data_type}'. "
            f"All entities should now use the new modular service pattern."
        )
        raise ValueError(error_msg)

    # No matching handler
    error_msg = f"No handler for data_type '{data_type}' with service {type(service).__name__}"
    raise ValueError(error_msg)
