"""Salesforce Opportunity-field to internal CRMDeal column map."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

_RAW_SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS: dict[str, str] = {
    "Name": "name",
    "Amount": "amount",
    "CurrencyIsoCode": "currency",
    "CloseDate": "close_date",
    "StageName": "stage_external_id",
    "OwnerId": "owner_external_id",
    "Type": "deal_type",
    "Description": "description",
    "ForecastCategoryName": "forecast_category",
    "Probability": "probability",
    "ExpectedRevenue": "predicted_amount",
    "CreatedDate": "source_created_at",
    "LastModifiedDate": "source_modified_at",
    "LastActivityDate": "last_activity_at",
    "DaysToClose": "days_to_close",
}


def _validate_mapping(raw: Mapping[str, str]) -> None:
    for field_name, canonical_column in raw.items():
        if not field_name:
            raise ValueError("Salesforce Opportunity field key must be non-empty")
        if not hasattr(CRMDeal, canonical_column):
            raise ValueError(
                f"Canonical column {canonical_column!r} does not exist on CRMDeal "
                f"(mapped from Salesforce field {field_name!r})"
            )


_validate_mapping(_RAW_SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS)

SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS: Mapping[str, str] = MappingProxyType(
    _RAW_SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS
)


def is_salesforce_opportunity_canonical(field_name: str) -> bool:
    return field_name in SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS


def canonical_column_for_salesforce_opportunity(field_name: str) -> str | None:
    return SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS.get(field_name)
