"""Salesforce Opportunity -> NormalizedSalesforceOpportunity."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    NormalizedSalesforceOpportunity,
)
from src.service.integrations.crm.schema.salesforce.opportunities.mapping import (
    SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS,
)

logger = get_logger(__name__)


def normalize_opportunities(
    raw_opportunities: Iterable[dict[str, Any]],
    *,
    instance_url: str,
    primary_contact_by_deal: Mapping[str, str],
    current_stage_entered_at_by_deal: Mapping[str, datetime],
    contact_count_by_deal: Mapping[str, int],
) -> tuple[list[NormalizedSalesforceOpportunity], int]:
    normalized: list[NormalizedSalesforceOpportunity] = []
    skipped_malformed = 0
    for raw in raw_opportunities:
        opportunity = _normalize_one(
            raw,
            instance_url=instance_url,
            primary_contact_by_deal=primary_contact_by_deal,
            current_stage_entered_at_by_deal=current_stage_entered_at_by_deal,
            contact_count_by_deal=contact_count_by_deal,
        )
        if opportunity is None:
            skipped_malformed += 1
            continue
        normalized.append(opportunity)
    return normalized, skipped_malformed


def _normalize_one(
    raw: object,
    *,
    instance_url: str,
    primary_contact_by_deal: Mapping[str, str],
    current_stage_entered_at_by_deal: Mapping[str, datetime],
    contact_count_by_deal: Mapping[str, int],
) -> NormalizedSalesforceOpportunity | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict Salesforce opportunity payload")
        return None

    external_id = _coerce_id(raw.get("Id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed Salesforce opportunity (no usable Id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    pipeline_external_id = _pipeline_external_id(raw.get("RecordTypeId"))
    stage_name = _coerce_str(raw.get("StageName"))
    stage_external_id = (
        f"{pipeline_external_id}:{stage_name}" if pipeline_external_id and stage_name else None
    )
    deal_status = _derive_deal_status(raw)
    stage_entered_at = current_stage_entered_at_by_deal.get(external_id)

    return NormalizedSalesforceOpportunity(
        external_id=external_id,
        name=_coerce_str(raw.get("Name")),
        amount=_coerce_float(raw.get("Amount")),
        amount_home_currency=None,
        currency=_coerce_str(raw.get("CurrencyIsoCode")),
        deal_status=deal_status,
        probability=_coerce_probability(raw.get("Probability")),
        predicted_amount=_coerce_float(raw.get("ExpectedRevenue")),
        close_date=_parse_salesforce_datetime_or_date(raw.get("CloseDate")),
        expected_close_date=None,
        closed_won_at=(stage_entered_at if deal_status == CRMDealStatus.WON else None),
        closed_lost_at=(stage_entered_at if deal_status == CRMDealStatus.LOST else None),
        closed_reason=None,
        crm_url=_build_crm_url(instance_url, external_id),
        pipeline_external_id=pipeline_external_id,
        stage_external_id=stage_external_id,
        forecast_category=_coerce_str(raw.get("ForecastCategoryName")),
        deal_type=_coerce_str(raw.get("Type")),
        priority=None,
        lead_source=_coerce_str(raw.get("LeadSource")),
        owner_external_id=_coerce_str(raw.get("OwnerId")),
        created_by_user_external_id=_coerce_str(raw.get("CreatedById")),
        updated_by_user_external_id=_coerce_str(raw.get("LastModifiedById")),
        primary_company_external_id=_coerce_str(raw.get("AccountId")),
        primary_contact_external_id=primary_contact_by_deal.get(external_id),
        source_created_at=_parse_salesforce_timestamp(raw.get("CreatedDate")),
        source_modified_at=_parse_salesforce_timestamp(raw.get("LastModifiedDate")),
        last_activity_at=_parse_salesforce_datetime_or_date(raw.get("LastActivityDate")),
        stage_entered_at=stage_entered_at,
        stage_updated_at=_parse_salesforce_timestamp(raw.get("LastModifiedDate")),
        days_to_close=_coerce_int(raw.get("DaysToClose")),
        associated_contact_count=contact_count_by_deal.get(external_id, 0),
        associated_company_count=(1 if _coerce_str(raw.get("AccountId")) else 0),
        note_count=None,
        description=_coerce_str(raw.get("Description")),
        properties=_collect_overflow_properties(raw),
        metrics={},
        associations_payload={
            "primary_contact_external_id": primary_contact_by_deal.get(external_id),
            "company_external_id": _coerce_str(raw.get("AccountId")),
            "contact_count": contact_count_by_deal.get(external_id, 0),
        },
        data_payload=raw,
    )


def _pipeline_external_id(value: Any) -> str:
    record_type_id = _coerce_str(value)
    return record_type_id or "master"


def _derive_deal_status(raw: Mapping[str, Any]) -> CRMDealStatus:
    is_won = _coerce_bool(raw.get("IsWon"))
    is_closed = _coerce_bool(raw.get("IsClosed"))
    if is_won:
        return CRMDealStatus.WON
    if is_closed:
        return CRMDealStatus.LOST
    return CRMDealStatus.OPEN


def _collect_overflow_properties(raw: dict[str, Any]) -> dict[str, Any]:
    canonical = set(SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS.keys()) | {
        "Id",
        "IsClosed",
        "IsWon",
        "RecordTypeId",
        "AccountId",
        "CreatedById",
        "LastModifiedById",
        "attributes",
    }
    return {
        "salesforce_object": "Opportunity",
        "raw_fields": {key: value for key, value in raw.items() if key not in canonical},
    }


def _build_crm_url(instance_url: str, external_id: str) -> str:
    return f"{instance_url.rstrip('/')}/lightning/r/Opportunity/{external_id}/view"


def _coerce_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, int | float):
        return str(value)
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _coerce_probability(value: Any) -> float | None:
    as_float = _coerce_float(value)
    if as_float is None:
        return None
    if as_float < 0.0:
        return 0.0
    if as_float > 100.0:
        return 100.0
    return as_float


def _parse_salesforce_datetime_or_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) == 10:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(parsed, time.min, tzinfo=UTC)
    return _parse_salesforce_timestamp(value)


def _parse_salesforce_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value
    if len(cleaned) >= 5 and cleaned[-5] in {"+", "-"} and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None
