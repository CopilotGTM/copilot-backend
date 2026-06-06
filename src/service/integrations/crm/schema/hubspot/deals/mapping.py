"""HubSpot deal-property to internal CRMDeal column map.

Single source of truth for which HubSpot deal properties are already canonical
on our side. Any HubSpot property present in this map is excluded from
crm_custom_field_definitions during schema sync.

Editing rule: add/remove entries here when CRMDeal gains or loses a canonical
column, or when we confirm a HubSpot property name whose semantics match an
existing CRMDeal column. Do not classify by label similarity; keys are exact
HubSpot property names as returned by the Properties API.

This file is also the field-level data map that the future HubSpot record-sync
phase will use to populate CRMDeal columns from HubSpot payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

_RAW_HUBSPOT_DEAL_CANONICAL_FIELDS: dict[str, str] = {
    "dealname": "name",
    "amount": "amount",
    "deal_currency_code": "currency",
    "amount_in_home_currency": "amount_home_currency",
    "closedate": "close_date",
    "dealtype": "deal_type",
    "description": "description",
    "hs_forecast_category": "forecast_category",
    "hs_priority": "priority",
    "hs_deal_stage_probability": "probability",
    "hs_projected_amount": "predicted_amount",
    "createdate": "source_created_at",
    "hs_lastmodifieddate": "source_modified_at",
    "hs_date_entered_current_stage": "stage_entered_at",
    "notes_last_updated": "last_activity_at",
    "days_to_close": "days_to_close",
    "closed_won_reason": "closed_reason",
    "closed_lost_reason": "closed_reason",
    "num_associated_contacts": "associated_contact_count",
    "num_notes": "note_count",
}

_RAW_HUBSPOT_DEAL_IGNORED_FIELDS: set[str] = {
    "closed_won_date",
    "closed_lost_date",
    "dealstage",
    "hubspot_owner_id",
    "pipeline",
}


def _validate_mapping(raw: Mapping[str, str]) -> None:
    """Fail fast at import time if the map drifts from CRMDeal model reality."""

    seen_keys: set[str] = set()
    for hubspot_property, canonical_column in raw.items():
        if hubspot_property != hubspot_property.lower() or not hubspot_property:
            raise ValueError(
                f"HubSpot property key must be non-empty lowercase: {hubspot_property!r}"
            )
        if hubspot_property in seen_keys:
            raise ValueError(f"Duplicate HubSpot property key: {hubspot_property!r}")
        seen_keys.add(hubspot_property)

        if not hasattr(CRMDeal, canonical_column):
            raise ValueError(
                f"Canonical column {canonical_column!r} does not exist on CRMDeal "
                f"(mapped from HubSpot property {hubspot_property!r})"
            )


_validate_mapping(_RAW_HUBSPOT_DEAL_CANONICAL_FIELDS)

HUBSPOT_DEAL_CANONICAL_FIELDS: Mapping[str, str] = MappingProxyType(
    _RAW_HUBSPOT_DEAL_CANONICAL_FIELDS
)
HUBSPOT_DEAL_IGNORED_FIELDS: frozenset[str] = frozenset(_RAW_HUBSPOT_DEAL_IGNORED_FIELDS)


def is_hubspot_deal_canonical(property_name: str) -> bool:
    """True if the HubSpot property name already maps to a canonical CRMDeal column."""
    return property_name in HUBSPOT_DEAL_CANONICAL_FIELDS


def canonical_column_for(property_name: str) -> str | None:
    """Return the CRMDeal attribute name for a HubSpot property, or None if non-canonical."""
    return HUBSPOT_DEAL_CANONICAL_FIELDS.get(property_name)


def is_hubspot_deal_ignored(property_name: str) -> bool:
    """True if the HubSpot property should be excluded from canonical and custom-field sync."""
    return property_name in HUBSPOT_DEAL_IGNORED_FIELDS
