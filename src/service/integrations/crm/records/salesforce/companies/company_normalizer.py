"""Salesforce Account -> NormalizedSalesforceCompany."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.companies.company_types import (
    NormalizedSalesforceCompany,
)

logger = get_logger(__name__)


def normalize_accounts(
    raw_accounts: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedSalesforceCompany], int]:
    normalized: list[NormalizedSalesforceCompany] = []
    skipped_malformed = 0
    for raw in raw_accounts:
        company = _normalize_one(raw)
        if company is None:
            skipped_malformed += 1
            continue
        normalized.append(company)
    return normalized, skipped_malformed


def _normalize_one(raw: object) -> NormalizedSalesforceCompany | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict Salesforce account payload")
        return None

    external_id = _coerce_id(raw.get("Id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed Salesforce account (no usable Id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    website = _coerce_str(raw.get("Website"))
    return NormalizedSalesforceCompany(
        external_id=external_id,
        name=_coerce_str(raw.get("Name")),
        domain=_extract_domain(website),
        website=website,
        description=_coerce_str(raw.get("Description")),
        industry=_coerce_str(raw.get("Industry")),
        company_type=_coerce_str(raw.get("Type")),
        lifecycle_stage=None,
        owner_external_id=_coerce_str(raw.get("OwnerId")),
        number_of_employees=_coerce_int(raw.get("NumberOfEmployees")),
        annual_revenue=_coerce_float(raw.get("AnnualRevenue")),
        phone=_coerce_str(raw.get("Phone")),
        city=_coerce_str(raw.get("BillingCity")) or _coerce_str(raw.get("ShippingCity")),
        state=_coerce_str(raw.get("BillingState")) or _coerce_str(raw.get("ShippingState")),
        country=_coerce_str(raw.get("BillingCountry")) or _coerce_str(raw.get("ShippingCountry")),
        address=_coerce_str(raw.get("BillingStreet")) or _coerce_str(raw.get("ShippingStreet")),
        postal_code=_coerce_str(raw.get("BillingPostalCode"))
        or _coerce_str(raw.get("ShippingPostalCode")),
        timezone=None,
        linkedin_url=_coerce_str(raw.get("LinkedIn__c")),
        source_created_at=_parse_salesforce_timestamp(raw.get("CreatedDate")),
        source_modified_at=_parse_salesforce_timestamp(raw.get("LastModifiedDate")),
        properties={
            "salesforce_object": "Account",
            "raw_fields": raw,
        },
        data_payload=raw,
    )


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


def _extract_domain(website: str | None) -> str | None:
    if website is None:
        return None
    candidate = website if "://" in website else f"https://{website}"
    parsed = urlparse(candidate)
    domain = parsed.netloc or parsed.path
    domain = domain.strip().lower()
    if not domain:
        return None
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


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
