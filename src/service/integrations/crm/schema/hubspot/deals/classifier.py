"""Pure classification of raw HubSpot deal properties.

Given a raw HubSpot property dict, decide whether it maps to an existing
canonical CRMDeal column or becomes a provider-backed custom-field row.
No I/O, no session, no normalization.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.service.integrations.crm.schema.common.types import (
    CanonicalMatch,
    Classification,
    IgnoredMatch,
    ProviderCustomMatch,
)
from src.service.integrations.crm.schema.hubspot.deals.mapping import (
    canonical_column_for,
    is_hubspot_deal_ignored,
)


class MalformedHubSpotProperty(ValueError):  # noqa: N818
    """Raised for malformed HubSpot property payloads.

    We fail fast here so invalid entries are skipped as metadata drift rather than
    accidentally inserted into the custom-field registry.
    """


def classify(raw: Mapping[str, Any]) -> Classification:
    """Classify one raw HubSpot property into one of three sync categories.

    - `CanonicalMatch`: write to CRMDeal model directly, no custom registry row
    - `IgnoredMatch`: drop intentionally (noise or derived fields)
    - `ProviderCustomMatch`: normalize and persist as provider-backed field
    """

    name_value = raw.get("name")
    if not isinstance(name_value, str) or not name_value:
        raise MalformedHubSpotProperty(f"HubSpot property missing non-empty string 'name': {raw!r}")

    if is_hubspot_deal_ignored(name_value):
        return IgnoredMatch(provider_field_key=name_value)

    column = canonical_column_for(name_value)
    if column is not None:
        return CanonicalMatch(provider_field_key=name_value, canonical_column=column)

    return ProviderCustomMatch(
        provider_field_key=name_value,
        is_provider_custom=not bool(raw.get("hubspotDefined", False)),
    )
