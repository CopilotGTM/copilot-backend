"""HubSpot task webhook subscription names and property filters.

HubSpot delivers task webhooks through the generic expanded webhook format:
`object.*` with `objectTypeId = 0-27`. The route normalizes those provider
events into these internal `task.*` names before dispatching to workers.
"""

from __future__ import annotations

from src.service.integrations.webhooks.hubspot.notes.subscriptions import (
    HUBSPOT_GENERIC_ASSOCIATION_CHANGE,
    HUBSPOT_GENERIC_CREATION,
    HUBSPOT_GENERIC_DELETION,
    HUBSPOT_GENERIC_PROPERTY_CHANGE,
)

HUBSPOT_TASK_OBJECT_TYPE_ID = "0-27"

TASK_CREATION = "task.creation"
TASK_PROPERTY_CHANGE = "task.propertyChange"
TASK_DELETION = "task.deletion"
TASK_ASSOCIATION_CHANGE = "task.associationChange"

HUBSPOT_GENERIC_TASK_SUBSCRIPTION_MAP = {
    HUBSPOT_GENERIC_CREATION: TASK_CREATION,
    HUBSPOT_GENERIC_PROPERTY_CHANGE: TASK_PROPERTY_CHANGE,
    HUBSPOT_GENERIC_DELETION: TASK_DELETION,
    HUBSPOT_GENERIC_ASSOCIATION_CHANGE: TASK_ASSOCIATION_CHANGE,
}

TASK_PROPERTY_CHANGE_PROPERTIES: tuple[str, ...] = (
    "hs_task_subject",
    "hs_task_body",
    "hs_task_status",
    "hs_task_priority",
    "hs_task_type",
    "hs_timestamp",
    "hubspot_owner_id",
)
TASK_PROPERTY_CHANGE_PROPERTY_SET = frozenset(TASK_PROPERTY_CHANGE_PROPERTIES)

TASK_SUBSCRIPTIONS = (
    TASK_CREATION,
    TASK_PROPERTY_CHANGE,
    TASK_DELETION,
    TASK_ASSOCIATION_CHANGE,
)
