"""HubSpot CRM provider."""

from __future__ import annotations

from typing import Any

from src.service.integrations.nango_client import NangoClient

from .fetchers import (
    HubSpotCompanyFetcher,
    HubSpotContactFetcher,
    HubSpotDealFetcher,
    HubSpotDealTaskFetcher,
    HubSpotOwnerFetcher,
    HubSpotPipelineFetcher,
    HubSpotUserFetcher,
)

__all__ = [
    "HubSpotCompanyFetcher",
    "HubSpotContactFetcher",
    "HubSpotDealFetcher",
    "HubSpotDealTaskFetcher",
    "HubSpotOwnerFetcher",
    "HubSpotPipelineFetcher",
    "HubSpotUserFetcher",
    "fetch_companies",
    "fetch_contacts",
    "fetch_deals",
    "fetch_deal_tasks",
    "fetch_owners",
    "fetch_pipelines",
    "fetch_users",
]


# Wrapper functions for registry compatibility
async def fetch_companies(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001 - Required by registry interface
    data_type: str | None,  # noqa: ARG001 - Required by registry interface
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch companies from HubSpot."""
    # Parameters last_synced_at and data_type are part of registry interface
    # but not used in this HubSpot-specific wrapper
    _ = last_synced_at, data_type
    fetcher = HubSpotCompanyFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial)


async def fetch_contacts(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch contacts from HubSpot."""
    _ = last_synced_at, data_type
    fetcher = HubSpotContactFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial)


async def fetch_deals(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch deals from HubSpot."""
    _ = last_synced_at, data_type
    fetcher = HubSpotDealFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial)


async def fetch_owners(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch owners from HubSpot."""
    _ = last_synced_at, data_type
    fetcher = HubSpotOwnerFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial)


async def fetch_pipelines(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch pipelines from HubSpot."""
    _ = last_synced_at, data_type
    fetcher = HubSpotPipelineFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial)


async def fetch_users(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch users from HubSpot."""
    _ = last_synced_at, data_type
    fetcher = HubSpotUserFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial)


async def fetch_deal_tasks(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch deal tasks from HubSpot."""
    _ = last_synced_at, data_type
    fetcher = HubSpotDealTaskFetcher(nango, provider_config_key, connection_id)
    return await fetcher.fetch_all(incremental=not is_initial)
