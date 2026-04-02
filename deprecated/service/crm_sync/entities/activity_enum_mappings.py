"""Canonical enum mappings for cross-CRM activity normalization.

This module provides mapping tables that normalize provider-specific activity
status, type, and priority values to canonical enum values defined in
`CRMActivityStatus`, `CRMActivityType`, and `CRMActivityPriority`.

These mappings are used by sync services to ensure consistent analytics
across multiple CRM providers.
"""

from __future__ import annotations

from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivityStatus,
    CRMActivityType,
)

# ─────────────────────────────────────────────────────────────────────────────
# Status Mappings
# Note: Some CRMs share identical status names (Salesforce/Zoho), so we only
# need to list each unique string once.
# ─────────────────────────────────────────────────────────────────────────────

ACTIVITY_STATUS_MAP: dict[str, CRMActivityStatus] = {
    # Canonical / HubSpot (uppercase)
    "NOT_STARTED": CRMActivityStatus.NOT_STARTED,
    "IN_PROGRESS": CRMActivityStatus.IN_PROGRESS,
    "COMPLETED": CRMActivityStatus.COMPLETED,
    "WAITING": CRMActivityStatus.WAITING,
    "DEFERRED": CRMActivityStatus.DEFERRED,
    "CANCELLED": CRMActivityStatus.CANCELLED,
    # Salesforce / Zoho (title case)
    "Not Started": CRMActivityStatus.NOT_STARTED,
    "In Progress": CRMActivityStatus.IN_PROGRESS,
    "Completed": CRMActivityStatus.COMPLETED,
    "Waiting on someone else": CRMActivityStatus.WAITING,
    "Waiting for input": CRMActivityStatus.WAITING,
    "Deferred": CRMActivityStatus.DEFERRED,
    # Pipedrive (done flag based)
    "0": CRMActivityStatus.NOT_STARTED,  # done=false
    "1": CRMActivityStatus.COMPLETED,  # done=true
    "false": CRMActivityStatus.NOT_STARTED,
    "true": CRMActivityStatus.COMPLETED,
    # Common lowercase variants
    "not_started": CRMActivityStatus.NOT_STARTED,
    "in_progress": CRMActivityStatus.IN_PROGRESS,
    "completed": CRMActivityStatus.COMPLETED,
    "waiting": CRMActivityStatus.WAITING,
    "deferred": CRMActivityStatus.DEFERRED,
    "cancelled": CRMActivityStatus.CANCELLED,
    "canceled": CRMActivityStatus.CANCELLED,
}


# ─────────────────────────────────────────────────────────────────────────────
# Type Mappings
# ─────────────────────────────────────────────────────────────────────────────

ACTIVITY_TYPE_MAP: dict[str, CRMActivityType] = {
    # Canonical / HubSpot (uppercase)
    "TASK": CRMActivityType.TASK,
    "TODO": CRMActivityType.TODO,
    "CALL": CRMActivityType.CALL,
    "EMAIL": CRMActivityType.EMAIL,
    "MEETING": CRMActivityType.MEETING,
    "NOTE": CRMActivityType.NOTE,
    "EVENT": CRMActivityType.EVENT,
    "SMS": CRMActivityType.SMS,
    "LINKEDIN_MESSAGE": CRMActivityType.LINKEDIN_MESSAGE,
    "CHAT": CRMActivityType.CHAT,
    "OTHER": CRMActivityType.OTHER,
    # Salesforce / Zoho (title case)
    "Task": CRMActivityType.TASK,
    "Event": CRMActivityType.EVENT,
    "Call": CRMActivityType.CALL,
    "Email": CRMActivityType.EMAIL,
    "Meeting": CRMActivityType.MEETING,
    # Pipedrive (lowercase)
    "call": CRMActivityType.CALL,
    "meeting": CRMActivityType.MEETING,
    "task": CRMActivityType.TASK,
    "deadline": CRMActivityType.TASK,
    "email": CRMActivityType.EMAIL,
    "lunch": CRMActivityType.MEETING,
    "sms": CRMActivityType.SMS,
    "linkedin_message": CRMActivityType.LINKEDIN_MESSAGE,
    "chat": CRMActivityType.CHAT,
    "note": CRMActivityType.NOTE,
    "other": CRMActivityType.OTHER,
}


# ─────────────────────────────────────────────────────────────────────────────
# Priority Mappings
# ─────────────────────────────────────────────────────────────────────────────

ACTIVITY_PRIORITY_MAP: dict[str, CRMActivityPriority] = {
    # Canonical / HubSpot (uppercase)
    "NONE": CRMActivityPriority.NONE,
    "LOW": CRMActivityPriority.LOW,
    "MEDIUM": CRMActivityPriority.MEDIUM,
    "HIGH": CRMActivityPriority.HIGH,
    "URGENT": CRMActivityPriority.URGENT,
    # Salesforce / Zoho (title case)
    "Normal": CRMActivityPriority.MEDIUM,
    "High": CRMActivityPriority.HIGH,
    "Low": CRMActivityPriority.LOW,
    "Highest": CRMActivityPriority.URGENT,
    "Lowest": CRMActivityPriority.LOW,
    # Pipedrive / common lowercase
    "none": CRMActivityPriority.NONE,
    "low": CRMActivityPriority.LOW,
    "medium": CRMActivityPriority.MEDIUM,
    "high": CRMActivityPriority.HIGH,
    "urgent": CRMActivityPriority.URGENT,
    "normal": CRMActivityPriority.MEDIUM,
}


def normalize_activity_status(raw_status: str | None) -> str | None:
    """Normalize a provider-specific status to canonical CRMActivityStatus value.

    Args:
        raw_status: Raw status string from provider

    Returns:
        Canonical status string, or None if the value is unknown.
    """
    if not raw_status:
        return None

    mapped = ACTIVITY_STATUS_MAP.get(raw_status)
    if mapped:
        return mapped.value

    # Try case-insensitive lookup
    mapped = ACTIVITY_STATUS_MAP.get(raw_status.upper())
    if mapped:
        return mapped.value

    # Unknown statuses should not be written to DB-constrained columns.
    # Preserve the raw value elsewhere (e.g., properties) if needed.
    return None


def normalize_activity_type(raw_type: str | None) -> str | None:
    """Normalize a provider-specific type to canonical CRMActivityType value.

    Args:
        raw_type: Raw type string from provider

    Returns:
        Canonical type string, or OTHER when the value is unknown.
    """
    if not raw_type:
        return None

    mapped = ACTIVITY_TYPE_MAP.get(raw_type)
    if mapped:
        return mapped.value

    # Try case-insensitive lookup
    mapped = ACTIVITY_TYPE_MAP.get(raw_type.upper())
    if mapped:
        return mapped.value

    # Unknown types should not be written to DB-constrained columns.
    # Prefer OTHER and preserve the raw value in activity_subtype/properties.
    return CRMActivityType.OTHER.value


def normalize_activity_priority(raw_priority: str | None) -> str | None:
    """Normalize a provider-specific priority to canonical CRMActivityPriority value.

    Args:
        raw_priority: Raw priority string from provider

    Returns:
        Canonical priority string, or NONE when the value is unknown.
    """
    if not raw_priority:
        return None

    mapped = ACTIVITY_PRIORITY_MAP.get(raw_priority)
    if mapped:
        return mapped.value

    # Try case-insensitive lookup
    mapped = ACTIVITY_PRIORITY_MAP.get(raw_priority.upper())
    if mapped:
        return mapped.value

    # Unknown priorities should not be written to DB-constrained columns.
    # Prefer NONE and preserve the raw value in properties if needed.
    return CRMActivityPriority.NONE.value
