"""HubSpot note webhook subscription names and property filters.

HubSpot delivers note webhooks through the generic expanded webhook format:
`object.*` with `objectTypeId = 0-46`. The route normalizes those provider
events into these internal `note.*` names before dispatching to workers.
"""

from __future__ import annotations

HUBSPOT_NOTE_OBJECT_TYPE_ID = "0-46"

HUBSPOT_GENERIC_CREATION = "object.creation"
HUBSPOT_GENERIC_PROPERTY_CHANGE = "object.propertyChange"
HUBSPOT_GENERIC_DELETION = "object.deletion"
HUBSPOT_GENERIC_ASSOCIATION_CHANGE = "object.associationChange"

NOTE_CREATION = "note.creation"
NOTE_PROPERTY_CHANGE = "note.propertyChange"
NOTE_DELETION = "note.deletion"
NOTE_ASSOCIATION_CHANGE = "note.associationChange"

HUBSPOT_GENERIC_NOTE_SUBSCRIPTION_MAP = {
    HUBSPOT_GENERIC_CREATION: NOTE_CREATION,
    HUBSPOT_GENERIC_PROPERTY_CHANGE: NOTE_PROPERTY_CHANGE,
    HUBSPOT_GENERIC_DELETION: NOTE_DELETION,
    HUBSPOT_GENERIC_ASSOCIATION_CHANGE: NOTE_ASSOCIATION_CHANGE,
}

HUBSPOT_GENERIC_OBJECT_SUBSCRIPTIONS = frozenset(HUBSPOT_GENERIC_NOTE_SUBSCRIPTION_MAP)

NOTE_PROPERTY_CHANGE_PROPERTIES: tuple[str, ...] = (
    "hs_note_body",
    "hubspot_owner_id",
    "hs_attachment_ids",
    "hs_timestamp",
)
NOTE_PROPERTY_CHANGE_PROPERTY_SET = frozenset(NOTE_PROPERTY_CHANGE_PROPERTIES)

NOTE_SUBSCRIPTIONS = (
    NOTE_CREATION,
    NOTE_PROPERTY_CHANGE,
    NOTE_DELETION,
    NOTE_ASSOCIATION_CHANGE,
)
