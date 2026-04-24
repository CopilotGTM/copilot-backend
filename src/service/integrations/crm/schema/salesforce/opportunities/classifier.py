"""Pure classification of raw Salesforce Opportunity describe fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.service.integrations.crm.schema.common.types import (
    CanonicalMatch,
    Classification,
    ProviderCustomMatch,
)
from src.service.integrations.crm.schema.salesforce.opportunities.mapping import (
    canonical_column_for_salesforce_opportunity,
)


class MalformedSalesforceField(ValueError):  # noqa: N818
    """Raised for malformed Salesforce describe field payloads."""


def classify_salesforce_opportunity_field(raw: Mapping[str, Any]) -> Classification:
    field_name = raw.get("name")
    if not isinstance(field_name, str) or not field_name:
        raise MalformedSalesforceField(f"Salesforce field missing non-empty string 'name': {raw!r}")

    column = canonical_column_for_salesforce_opportunity(field_name)
    if column is not None:
        return CanonicalMatch(provider_field_key=field_name, canonical_column=column)

    return ProviderCustomMatch(
        provider_field_key=field_name,
        is_provider_custom=bool(raw.get("custom", False)),
    )
