"""Utilities for managing sync metadata and action classification.

This module provides helpers to:
1. Classify records into sync actions (ADDED, UPDATED, DELETED)
2. Add synthetic _sync_metadata to records for incremental processing
3. Log sync action distribution for observability

Since we fetch directly from CRM APIs (not Nango's Records API), we don't receive
_nango_metadata automatically. This module provides equivalent functionality by
inferring sync actions from record state and context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class SyncAction(str, Enum):
    """Classification of a record's sync action.

    These mirror Nango's _nango_metadata.last_action values:
    - ADDED: New record created in source system
    - UPDATED: Existing record modified
    - DELETED: Record deleted/archived in source system
    - UNCHANGED: Record fetched but no changes detected (for future optimization)
    """

    ADDED = "ADDED"
    UPDATED = "UPDATED"
    DELETED = "DELETED"
    UNCHANGED = "UNCHANGED"


@dataclass
class SyncActionCounts:
    """Tracks counts of records by sync action."""

    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    total: int = 0

    def increment(self, action: SyncAction) -> None:
        """Increment counter for a specific action."""
        self.total += 1
        if action == SyncAction.ADDED:
            self.added += 1
        elif action == SyncAction.UPDATED:
            self.updated += 1
        elif action == SyncAction.DELETED:
            self.deleted += 1
        elif action == SyncAction.UNCHANGED:
            self.unchanged += 1

    def to_dict(self) -> dict[str, int]:
        """Convert to dictionary for logging/serialization."""
        return {
            "added": self.added,
            "updated": self.updated,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "total": self.total,
        }


@dataclass
class SyncContext:
    """Context information for a sync operation.

    Attributes:
        sync_type: Type of sync (INITIAL or INCREMENTAL)
        is_initial: Whether this is an initial (full) sync
        response_results: Counts from Nango webhook (added, updated, deleted)
        existing_external_ids: Set of external IDs already in our database
    """

    sync_type: str | None = None
    is_initial: bool = False
    response_results: dict[str, int] = field(default_factory=dict)
    existing_external_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_params(
        cls,
        *,
        sync_type: str | None = None,
        is_initial: bool = False,
        response_results: dict[str, int] | None = None,
    ) -> SyncContext:
        """Create SyncContext from common parameters."""
        return cls(
            sync_type=sync_type,
            is_initial=is_initial or (sync_type and sync_type.upper() == "INITIAL"),
            response_results=response_results or {},
        )


def determine_sync_action(
    record: dict[str, Any],
    *,
    context: SyncContext | None = None,
    existing_ids: set[str] | None = None,
) -> SyncAction:
    """Determine the sync action for a record based on its state and context.

    Args:
        record: The record to classify
        context: Optional sync context with additional information
        existing_ids: Set of external IDs that already exist in our database

    Returns:
        SyncAction indicating what operation should be performed

    Logic:
    1. If record is archived/deleted -> DELETED
    2. If it's an INITIAL sync -> All non-deleted are ADDED
    3. If record's external_id exists in our DB -> UPDATED
    4. Otherwise -> ADDED
    """
    # Check for deletion/archival
    is_archived = record.get("archived", False)
    is_deleted = record.get("IsDeleted", False)  # Salesforce style

    if is_archived or is_deleted:
        return SyncAction.DELETED

    # For initial sync, all records are considered new
    if context and context.is_initial:
        return SyncAction.ADDED

    # Check if record exists in our database
    external_id = record.get("id") or record.get("Id")
    effective_existing_ids = existing_ids or (context.existing_external_ids if context else set())

    if external_id and str(external_id) in effective_existing_ids:
        return SyncAction.UPDATED

    # Default to ADDED for new records
    return SyncAction.ADDED


def add_sync_metadata(
    record: dict[str, Any],
    action: SyncAction,
    *,
    sync_type: str | None = None,
) -> dict[str, Any]:
    """Add synthetic sync metadata to a record.

    This creates a _sync_metadata field similar to Nango's _nango_metadata,
    allowing downstream processing to handle records uniformly regardless
    of data source.

    Args:
        record: The record to augment
        action: The determined sync action
        sync_type: The sync type (INITIAL or INCREMENTAL)

    Returns:
        The record with _sync_metadata added (mutates original)
    """
    record["_sync_metadata"] = {
        "sync_action": action.value,
        "sync_type": sync_type,
        "inferred": True,  # Indicates this is synthetic, not from Nango
    }
    return record


def classify_records(
    records: list[dict[str, Any]],
    *,
    context: SyncContext | None = None,
    existing_ids: set[str] | None = None,
    add_metadata: bool = True,
) -> tuple[list[dict[str, Any]], SyncActionCounts]:
    """Classify all records and optionally add sync metadata.

    Args:
        records: List of records to classify
        context: Sync context with operation details
        existing_ids: Set of external IDs already in database
        add_metadata: Whether to add _sync_metadata to each record

    Returns:
        Tuple of (processed records, action counts)
    """
    counts = SyncActionCounts()
    processed: list[dict[str, Any]] = []

    for record in records:
        action = determine_sync_action(record, context=context, existing_ids=existing_ids)
        counts.increment(action)

        if add_metadata:
            sync_type = context.sync_type if context else None
            add_sync_metadata(record, action, sync_type=sync_type)

        processed.append(record)

    return processed, counts


def log_sync_action_distribution(
    counts: SyncActionCounts,
    *,
    entity_type: str = "records",
    provider: str | None = None,
    sync_type: str | None = None,
) -> None:
    """Log the distribution of sync actions for observability.

    Args:
        counts: The action counts to log
        entity_type: Type of entity (deals, contacts, etc.)
        provider: Provider name (hubspot, salesforce, etc.)
        sync_type: Type of sync operation
    """
    logger.info(
        "Sync action distribution",
        extra={
            "entity_type": entity_type,
            "added": counts.added,
            "updated": counts.updated,
            "deleted": counts.deleted,
            "unchanged": counts.unchanged,
            "total": counts.total,
            "provider": provider or "unknown",
            "sync_type": sync_type or "unknown",
        },
    )


def extract_sync_action(record: dict[str, Any]) -> SyncAction | None:
    """Extract the sync action from a record's metadata.

    Checks both _sync_metadata (our synthetic) and _nango_metadata (if present).

    Args:
        record: The record to check

    Returns:
        SyncAction if found, None otherwise
    """
    # Check our synthetic metadata first
    sync_meta = record.get("_sync_metadata", {})
    if sync_meta:
        action_str = sync_meta.get("sync_action")
        if action_str:
            try:
                return SyncAction(action_str)
            except ValueError:
                pass

    # Check Nango metadata as fallback (if we ever use Records API)
    nango_meta = record.get("_nango_metadata", {})
    if nango_meta:
        action_str = nango_meta.get("last_action")
        if action_str:
            try:
                return SyncAction(action_str)
            except ValueError:
                pass

    return None


def filter_records_by_action(
    records: list[dict[str, Any]],
    actions: set[SyncAction],
) -> list[dict[str, Any]]:
    """Filter records to only those with specified sync actions.

    Args:
        records: List of records with sync metadata
        actions: Set of actions to include

    Returns:
        Filtered list of records
    """
    result: list[dict[str, Any]] = []

    for record in records:
        action = extract_sync_action(record)
        if action and action in actions:
            result.append(record)

    return result


__all__ = [
    "SyncAction",
    "SyncActionCounts",
    "SyncContext",
    "add_sync_metadata",
    "classify_records",
    "determine_sync_action",
    "extract_sync_action",
    "filter_records_by_action",
    "log_sync_action_distribution",
]
