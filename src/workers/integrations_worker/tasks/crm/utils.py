"""Shared utilities for CRM sync tasks."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from src.domain.models.organisation_tables.sync import IntegrationType

if TYPE_CHECKING:
    from src.domain.models.organisation_tables.sync import KnowledgeIntegration

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
