"""HubSpot company → NormalizedHubSpotCompany.

Pure functions, no I/O. HubSpot's `/crm/v3/objects/companies` returns each
company as `{id, properties: {...}, createdAt, updatedAt, archived}`; this
module flattens the relevant properties onto our model columns and keeps the
raw record in `data_payload`.

Numeric HubSpot properties (`numberofemployees`, `annualrevenue`) arrive as
strings — we parse them defensively and leave them null on any failure, to
avoid rejecting an otherwise-valid row over a dirty input.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.companies.company_types import (
    NormalizedHubSpotCompany,
)

logger = get_logger(__name__)


def normalize_companies(
    raw_companies: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedHubSpotCompany], int]:
    """Normalise every raw company. Returns (valid list, skipped_malformed)."""
    normalized: list[NormalizedHubSpotCompany] = []
    skipped_malformed = 0
    for raw in raw_companies:
        company = _normalize_one(raw)
        if company is None:
            skipped_malformed += 1
            continue
        normalized.append(company)
    return normalized, skipped_malformed


def _normalize_one(raw: object) -> NormalizedHubSpotCompany | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict HubSpot company payload")
        return None

    external_id = _coerce_id(raw.get("id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed HubSpot company (no usable id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    # Defensive: server-side archived=false should filter these, but catch
    # stragglers rather than persisting them as active.
    if raw.get("archived") is True:
        logger.warning(
            "Skipping archived HubSpot company",
            extra={"company_id": external_id},
        )
        return None

    props = raw.get("properties") or {}
    if not isinstance(props, dict):
        props = {}

    name = _coerce_str(props.get("name"))
    domain = _coerce_str(props.get("domain"))
    website = _coerce_str(props.get("website"))
    description = _coerce_str(props.get("description"))
    industry = _coerce_str(props.get("industry"))
    company_type = _coerce_str(props.get("type"))
    lifecycle_stage = _coerce_str(props.get("lifecyclestage"))
    owner_external_id = _coerce_str(props.get("hubspot_owner_id"))
    number_of_employees = _coerce_int(props.get("numberofemployees"))
    annual_revenue = _coerce_float(props.get("annualrevenue"))
    phone = _coerce_str(props.get("phone"))
    city = _coerce_str(props.get("city"))
    state = _coerce_str(props.get("state"))
    country = _coerce_str(props.get("country"))
    address = _coerce_str(props.get("address"))
    postal_code = _coerce_str(props.get("zip"))
    timezone = _coerce_str(props.get("timezone"))
    linkedin_url = _coerce_str(props.get("linkedin_company_page"))

    # Prefer HubSpot's object-level timestamps; fall back to property-level
    # `hs_createdate` / `hs_lastmodifieddate` when the envelope is absent.
    source_created_at = _parse_iso(raw.get("createdAt")) or _parse_iso(props.get("hs_createdate"))
    source_modified_at = _parse_iso(raw.get("updatedAt")) or _parse_iso(
        props.get("hs_lastmodifieddate")
    )

    # Curated subset of provider properties for cheap downstream queries.
    # `data_payload` below keeps the full envelope for replay.
    curated_properties: dict[str, Any] = {
        "hubspot_type": company_type,
        "industry": industry,
        "lifecycle_stage": lifecycle_stage,
        "raw_properties": props,
    }

    return NormalizedHubSpotCompany(
        external_id=external_id,
        name=name,
        domain=domain,
        website=website,
        description=description,
        industry=industry,
        company_type=company_type,
        lifecycle_stage=lifecycle_stage,
        owner_external_id=owner_external_id,
        number_of_employees=number_of_employees,
        annual_revenue=annual_revenue,
        phone=phone,
        city=city,
        state=state,
        country=country,
        address=address,
        postal_code=postal_code,
        timezone=timezone,
        linkedin_url=linkedin_url,
        source_created_at=source_created_at,
        source_modified_at=source_modified_at,
        properties=curated_properties,
        data_payload=raw,
    )


def _coerce_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int):
        return str(value)
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, int | float):
        return str(value)
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, TypeError):
            return None
    return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
