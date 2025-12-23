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
) -> dict[str, Any]:
    """Sync owners using new entity pattern."""
    return await sync_owners(
        service=service,
        integration=integration,
        records=records,
        provider=provider,
    )


async def _sync_deals(
    service: DealSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync deals using new entity pattern."""
    return await sync_deals(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
    )


async def _sync_contacts(
    service: ContactSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync contacts using new entity pattern."""
    return await sync_contacts(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
    )


async def _sync_companies(
    service: CompanySyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync companies using new entity pattern."""
    return await sync_companies(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
    )


async def _sync_users(
    service: UserSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync users using new entity pattern."""
    return await sync_users(
        service=service,
        integration=integration,
        records=list(records),
        provider=provider,
    )


async def _sync_pipelines(
    service: PipelineSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync pipelines using new entity pattern."""
    return await sync_pipelines(
        service=service,
        integration=integration,
        records=list(records),
        provider=provider,
    )


async def _sync_deal_tasks(
    service: DealTaskSyncService,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync deal tasks using new entity pattern."""
    return await sync_deal_tasks(
        db=service.db,
        organisation_id=str(service.organisation_id),
        integration=integration,
        records=list(records),
        provider=provider,
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
) -> dict[str, Any]:
    """Dispatch CRM data sync by data_type."""

    # Define dispatch table for new entity services
    entity_handlers = {
        ("owners", OwnerSyncService): _sync_owners,
        ("deals", DealSyncService): _sync_deals,
        ("opportunities", DealSyncService): _sync_deals,  # Alias for Salesforce
        ("deal_tasks", DealTaskSyncService): _sync_deal_tasks,
        ("tasks", DealTaskSyncService): _sync_deal_tasks,  # Alias for backward compatibility
        ("tickets", DealTaskSyncService): _sync_deal_tasks,  # Salesforce Cases alias
        ("notes", DealTaskSyncService): _sync_deal_tasks,
        ("contacts", ContactSyncService): _sync_contacts,
        ("leads", ContactSyncService): _sync_contacts,  # Treat leads as contact-like profiles
        ("companies", CompanySyncService): _sync_companies,
        (
            "accounts",
            CompanySyncService,
        ): _sync_companies,  # Alias for Salesforce (Account → companies)
        ("users", UserSyncService): _sync_users,
        ("pipelines", PipelineSyncService): _sync_pipelines,
    }

    # Check entity handlers first
    for (entity_type, service_class), handler in entity_handlers.items():
        if data_type == entity_type and isinstance(service, service_class):
            return await handler(service, integration, records, provider)  # type: ignore

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
