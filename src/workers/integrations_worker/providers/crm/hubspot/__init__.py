"""HubSpot CRM provider."""

from __future__ import annotations

from typing import Any

from src.service.integrations.nango_client import NangoClient

from .fetchers import (
    HubSpotCompanyFetcher,
    HubSpotContactFetcher,
    HubSpotDealFetcher,
    HubSpotDealTaskFetcher,
    HubSpotNoteFetcher,
    HubSpotOwnerFetcher,
    HubSpotPipelineFetcher,
    HubSpotUserFetcher,
)

__all__ = [
    "HubSpotCompanyFetcher",
    "HubSpotContactFetcher",
    "HubSpotDealFetcher",
    "HubSpotDealTaskFetcher",
    "HubSpotNoteFetcher",
    "HubSpotOwnerFetcher",
    "HubSpotPipelineFetcher",
    "HubSpotUserFetcher",
    "fetch_companies",
    "fetch_contacts",
    "fetch_deals",
    "fetch_deal_tasks",
    "fetch_notes",
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


def _hs_note_to_canonical_activity(note: dict[str, Any]) -> dict[str, Any]:
    """Convert a HubSpot note object into our canonical activity payload.

    We intentionally shape it like the existing HubSpot task payload so the
    existing DealTaskSyncService can persist it into crm_activities.
    """

    props = note.get("properties") if isinstance(note.get("properties"), dict) else {}
    body = props.get("hs_note_body")

    canonical_props: dict[str, Any] = dict(props)
    # Populate task-like fields expected by DealTaskSyncService.
    canonical_props.setdefault("hs_task_subject", None)
    canonical_props["hs_task_body"] = body
    # Force activity type normalization to NOTE.
    canonical_props["hs_task_type"] = canonical_props.get("hs_task_type") or "NOTE"

    return {
        "id": note.get("id"),
        "createdAt": note.get("createdAt"),
        "updatedAt": note.get("updatedAt"),
        "archived": bool(note.get("archived")),
        "properties": {k: v for k, v in canonical_props.items() if v is not None},
        "associations": note.get("associations") or {},
        "data_payload": note,
    }


async def fetch_notes(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch notes from HubSpot.

    Notes are returned as canonical activities (NOTE) and stored in crm_activities.
    """

    _ = data_type
    fetcher = HubSpotNoteFetcher(nango, provider_config_key, connection_id)
    # Keep parity with other HubSpot CRM fetchers: incremental based on integration sync.
    # last_synced_at is passed through for potential future use.
    notes = await fetcher.fetch_all(
        incremental=not is_initial,
        last_sync_time=str(last_synced_at) if last_synced_at is not None else None,
    )
    return [_hs_note_to_canonical_activity(n) for n in notes if isinstance(n, dict)]
