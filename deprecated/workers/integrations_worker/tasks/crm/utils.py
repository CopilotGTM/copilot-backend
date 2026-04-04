"""Shared utilities for CRM sync tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.sync import IntegrationType
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from src.domain.models.organisation_tables.sync import KnowledgeIntegration

logger = get_logger(__name__)

# CRM integration types to sync
CRM_INTEGRATION_TYPES = (
    IntegrationType.HUBSPOT,
    IntegrationType.SALESFORCE,
)


def get_last_sync_timestamp(
    integration: KnowledgeIntegration,
    entity_type: str,
) -> datetime | None:
    """Get the last sync timestamp for a specific CRM entity type.

    Args:
        integration: The KnowledgeIntegration instance
        entity_type: The entity type (e.g., 'deals', 'contacts', 'pipelines')

    Returns:
        The last sync timestamp as a datetime, or None if not set
    """
    metadata = integration.connection_metadata or {}
    timestamp_str = metadata.get(f"crm_{entity_type}_last_sync_at")
    if timestamp_str:
        try:
            # Handle both 'Z' suffix and '+00:00' timezone formats
            return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


@dataclass
class SyncStrategy:
    """Result of sync strategy determination."""

    is_initial: bool
    last_sync_at: datetime | None  # The cursor to use for incremental sync (with buffer applied)


async def get_sync_strategy(
    session: AsyncSession,
    integration_id: UUID,
    entity_type: str,  # "contacts" or "companies"
) -> SyncStrategy:
    """Determine full vs incremental sync using IntegrationSyncHistory as source of truth.

    Instead of reading connection_metadata (which can be polluted by other entity types),
    this queries IntegrationSyncHistory for the last SUCCESS record for this specific
    integration + entity_type combination.

    A 3x time buffer is applied to the cursor to ensure no records are missed due to
    clock skew or processing delays.

    Args:
        session: The async database session
        integration_id: The integration UUID
        entity_type: The entity type ("contacts" or "companies")

    Returns:
        SyncStrategy with is_initial flag and buffered cursor
    """
    stmt = (
        select(IntegrationSyncHistory)
        .where(
            IntegrationSyncHistory.integration_id == integration_id,
            IntegrationSyncHistory.status == SyncStatus.SUCCESS,
            IntegrationSyncHistory.sync_metadata["entity"].as_string() == entity_type,
        )
        .order_by(desc(IntegrationSyncHistory.started_at))
        .limit(1)
    )
    result = await session.execute(stmt)
    last_success = result.scalar_one_or_none()

    if last_success is None:
        logger.info(
            "get_sync_strategy: No SUCCESS history for integration=%s entity=%s → full sync",
            integration_id,
            entity_type,
        )
        return SyncStrategy(is_initial=True, last_sync_at=None)

    now = datetime.now(UTC)
    last_started = last_success.started_at
    # Ensure last_started is timezone-aware
    if last_started.tzinfo is None:
        last_started = last_started.replace(tzinfo=UTC)

    gap = now - last_started
    # 3x buffer: go back 2*gap from last_started (total window = 3*gap)
    buffered_cursor = last_started - 2 * gap

    # Cap: never go back more than 7 days from last_started
    min_cursor = last_started - timedelta(days=7)
    if buffered_cursor < min_cursor:
        buffered_cursor = min_cursor

    # Cap: never go forward past last_started
    if buffered_cursor > last_started:
        buffered_cursor = last_started

    logger.info(
        "get_sync_strategy: integration=%s entity=%s → incremental sync, "
        "last_success_started_at=%s gap=%s buffered_cursor=%s",
        integration_id,
        entity_type,
        last_started.isoformat(),
        gap,
        buffered_cursor.isoformat(),
    )
    return SyncStrategy(is_initial=False, last_sync_at=buffered_cursor)


def update_last_sync_timestamp(
    integration: KnowledgeIntegration,
    entity_type: str,
    timestamp: datetime,
) -> None:
    """Update the last sync timestamp for a specific CRM entity type.

    This modifies the integration's connection_metadata in-place.
    Caller is responsible for committing the changes.

    Args:
        integration: The KnowledgeIntegration instance
        entity_type: The entity type (e.g., 'deals', 'contacts', 'pipelines')
        timestamp: The timestamp to set
    """
    metadata = dict(integration.connection_metadata or {})
    metadata[f"crm_{entity_type}_last_sync_at"] = timestamp.isoformat()
    integration.connection_metadata = metadata


def get_provider_from_integration_type(integration_type: IntegrationType) -> str:
    """Map integration type to provider name.

    Args:
        integration_type: The IntegrationType enum value

    Returns:
        Provider name string ('hubspot' or 'salesforce')
    """
    if integration_type == IntegrationType.HUBSPOT:
        return "hubspot"
    elif integration_type == IntegrationType.SALESFORCE:
        return "salesforce"
    else:
        raise ValueError(f"Unknown CRM integration type: {integration_type}")


def timestamp_to_ms(dt: datetime) -> str:
    """Convert datetime to milliseconds string for HubSpot API.

    HubSpot uses Unix timestamp in milliseconds for filtering.

    Args:
        dt: The datetime to convert

    Returns:
        Unix timestamp in milliseconds as a string
    """
    return str(int(dt.timestamp() * 1000))


def get_sync_data_type(entity_type: str, provider: str) -> str:
    """Get the provider-specific data type name for an entity.

    Args:
        entity_type: The entity type (e.g., 'deals', 'contacts')
        provider: The provider name ('hubspot' or 'salesforce')

    Returns:
        The data type name expected by the provider
    """
    # Salesforce uses 'opportunities' instead of 'deals'
    if entity_type == "deals" and provider == "salesforce":
        return "opportunities"
    # Salesforce uses 'accounts' instead of 'companies'
    if entity_type == "companies" and provider == "salesforce":
        return "accounts"
    # Salesforce uses 'users' but HubSpot uses 'owners'
    if entity_type == "users" and provider == "hubspot":
        return "owners"
    return entity_type
