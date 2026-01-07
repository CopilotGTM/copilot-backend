"""HubSpot CRM provider."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncContext,
    classify_records,
    log_sync_action_distribution,
)

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

logger = get_logger(__name__)

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
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch companies from HubSpot.

    Args:
        nango: Nango client instance
        connection_id: Nango connection ID
        is_initial: Whether this is an initial (full) sync
        last_synced_at: Timestamp of last sync (unused, kept for interface)
        data_type: Data type being synced (unused, kept for interface)
        provider_config_key: Nango provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook {added, updated, deleted}

    Returns:
        List of company records with _sync_metadata added for incremental processing
    """
    _ = last_synced_at, data_type
    fetcher = HubSpotCompanyFetcher(nango, provider_config_key, connection_id)
    records = await fetcher.fetch_all(incremental=not is_initial)

    # Add sync metadata for incremental processing optimization
    context = SyncContext.from_params(
        sync_type=sync_type,
        is_initial=is_initial,
        response_results=response_results,
    )

    processed_records, counts = classify_records(
        records,
        context=context,
        add_metadata=True,
    )

    log_sync_action_distribution(
        counts,
        entity_type="companies",
        provider="hubspot",
        sync_type=sync_type,
    )

    logger.info(
        "HubSpot companies fetch completed: total=%d (webhook_expected: added=%d updated=%d deleted=%d)",
        len(processed_records),
        response_results.get("added", 0) if response_results else 0,
        response_results.get("updated", 0) if response_results else 0,
        response_results.get("deleted", 0) if response_results else 0,
    )

    return processed_records


async def fetch_contacts(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch contacts from HubSpot.

    Args:
        nango: Nango client instance
        connection_id: Nango connection ID
        is_initial: Whether this is an initial (full) sync
        last_synced_at: Timestamp of last sync (unused, kept for interface)
        data_type: Data type being synced (unused, kept for interface)
        provider_config_key: Nango provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook {added, updated, deleted}

    Returns:
        List of contact records with _sync_metadata added for incremental processing
    """
    _ = last_synced_at, data_type
    fetcher = HubSpotContactFetcher(nango, provider_config_key, connection_id)
    records = await fetcher.fetch_all(incremental=not is_initial)

    # Add sync metadata for incremental processing optimization
    context = SyncContext.from_params(
        sync_type=sync_type,
        is_initial=is_initial,
        response_results=response_results,
    )

    processed_records, counts = classify_records(
        records,
        context=context,
        add_metadata=True,
    )

    log_sync_action_distribution(
        counts,
        entity_type="contacts",
        provider="hubspot",
        sync_type=sync_type,
    )

    logger.info(
        "HubSpot contacts fetch completed: total=%d (webhook_expected: added=%d updated=%d deleted=%d)",
        len(processed_records),
        response_results.get("added", 0) if response_results else 0,
        response_results.get("updated", 0) if response_results else 0,
        response_results.get("deleted", 0) if response_results else 0,
    )

    return processed_records


async def fetch_deals(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch deals from HubSpot.

    Args:
        nango: Nango client instance
        connection_id: Nango connection ID
        is_initial: Whether this is an initial (full) sync
        last_synced_at: Timestamp of last sync (unused, kept for interface)
        data_type: Data type being synced (unused, kept for interface)
        provider_config_key: Nango provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook {added, updated, deleted}

    Returns:
        List of deal records with _sync_metadata added for incremental processing
    """
    _ = last_synced_at, data_type
    fetcher = HubSpotDealFetcher(nango, provider_config_key, connection_id)
    records = await fetcher.fetch_all(incremental=not is_initial)

    # Add sync metadata for incremental processing optimization
    context = SyncContext.from_params(
        sync_type=sync_type,
        is_initial=is_initial,
        response_results=response_results,
    )

    # Classify records and add _sync_metadata
    # Note: For HubSpot, archived=True records are already fetched separately
    # and will be classified as DELETED
    processed_records, counts = classify_records(
        records,
        context=context,
        add_metadata=True,
    )

    # Log action distribution for observability
    log_sync_action_distribution(
        counts,
        entity_type="deals",
        provider="hubspot",
        sync_type=sync_type,
    )

    logger.info(
        "HubSpot deals fetch completed: total=%d (webhook_expected: added=%d updated=%d deleted=%d)",
        len(processed_records),
        response_results.get("added", 0) if response_results else 0,
        response_results.get("updated", 0) if response_results else 0,
        response_results.get("deleted", 0) if response_results else 0,
    )

    return processed_records


async def fetch_owners(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch owners from HubSpot.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = last_synced_at, data_type
    fetcher = HubSpotOwnerFetcher(nango, provider_config_key, connection_id)
    owners = await fetcher.fetch_all(incremental=not is_initial)

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and owners:
        context = SyncContext(response_results=response_results)
        classify_records(owners, context)
        log_sync_action_distribution(owners, "owners", logger)

    return owners


async def fetch_pipelines(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch pipelines from HubSpot.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = last_synced_at, data_type
    fetcher = HubSpotPipelineFetcher(nango, provider_config_key, connection_id)
    pipelines = await fetcher.fetch_all(incremental=not is_initial)

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and pipelines:
        context = SyncContext(response_results=response_results)
        classify_records(pipelines, context)
        log_sync_action_distribution(pipelines, "pipelines", logger)

    return pipelines


async def fetch_users(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch users from HubSpot.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = last_synced_at, data_type
    fetcher = HubSpotUserFetcher(nango, provider_config_key, connection_id)
    users = await fetcher.fetch_all(incremental=not is_initial)

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and users:
        context = SyncContext(response_results=response_results)
        classify_records(users, context)
        log_sync_action_distribution(users, "users", logger)

    return users


async def fetch_deal_tasks(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,  # noqa: ARG001
    data_type: str | None,  # noqa: ARG001
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch deal tasks from HubSpot.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = last_synced_at, data_type
    fetcher = HubSpotDealTaskFetcher(nango, provider_config_key, connection_id)
    tasks = await fetcher.fetch_all(incremental=not is_initial)

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and tasks:
        context = SyncContext(response_results=response_results)
        classify_records(tasks, context)
        log_sync_action_distribution(tasks, "deal_tasks", logger)

    return tasks


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
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch notes from HubSpot.

    Notes are returned as canonical activities (NOTE) and stored in crm_activities.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = data_type
    fetcher = HubSpotNoteFetcher(nango, provider_config_key, connection_id)
    # Keep parity with other HubSpot CRM fetchers: incremental based on integration sync.
    # last_synced_at is passed through for potential future use.
    notes = await fetcher.fetch_all(
        incremental=not is_initial,
        last_sync_time=str(last_synced_at) if last_synced_at is not None else None,
    )
    canonical_notes = [_hs_note_to_canonical_activity(n) for n in notes if isinstance(n, dict)]

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and canonical_notes:
        context = SyncContext(response_results=response_results)
        classify_records(canonical_notes, context)
        log_sync_action_distribution(canonical_notes, "notes", logger)

    return canonical_notes
