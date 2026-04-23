"""Backward-compatible wrapper for the moved HubSpot deal mapping module."""

from src.service.integrations.crm.schema.hubspot.deals.mapping import (
    HUBSPOT_DEAL_CANONICAL_FIELDS,
    HUBSPOT_DEAL_IGNORED_FIELDS,
    canonical_column_for,
    is_hubspot_deal_canonical,
    is_hubspot_deal_ignored,
)

__all__ = [
    "HUBSPOT_DEAL_CANONICAL_FIELDS",
    "HUBSPOT_DEAL_IGNORED_FIELDS",
    "canonical_column_for",
    "is_hubspot_deal_canonical",
    "is_hubspot_deal_ignored",
]
