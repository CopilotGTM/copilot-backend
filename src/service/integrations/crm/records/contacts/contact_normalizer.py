"""HubSpot contact → NormalizedHubSpotContact.

Pure functions, no I/O. HubSpot's `/crm/v3/objects/contacts` returns each
contact as `{id, properties: {...}, createdAt, updatedAt, archived}`.

`associatedcompanyid` is the property that carries the primary company's
HubSpot id — we stash it in `company_external_id` and let the writer resolve
it to `company_id` against the in-session companies snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.contacts.contact_types import (
    NormalizedHubSpotContact,
)

logger = get_logger(__name__)


def normalize_contacts(
    raw_contacts: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedHubSpotContact], int]:
    """Normalise every raw contact. Returns (valid list, skipped_malformed)."""
    normalized: list[NormalizedHubSpotContact] = []
    skipped_malformed = 0
    for raw in raw_contacts:
        contact = _normalize_one(raw)
        if contact is None:
            skipped_malformed += 1
            continue
        normalized.append(contact)
    return normalized, skipped_malformed


def _normalize_one(raw: object) -> NormalizedHubSpotContact | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict HubSpot contact payload")
        return None

    external_id = _coerce_id(raw.get("id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed HubSpot contact (no usable id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    if raw.get("archived") is True:
        logger.warning(
            "Skipping archived HubSpot contact",
            extra={"contact_id": external_id},
        )
        return None

    props = raw.get("properties") or {}
    if not isinstance(props, dict):
        props = {}

    first_name = _coerce_str(props.get("firstname"))
    last_name = _coerce_str(props.get("lastname"))
    email = _normalize_email(props.get("email"))
    full_name = _compose_full_name(first_name, last_name, email)
    job_title = _coerce_str(props.get("jobtitle"))
    company_name = _coerce_str(props.get("company"))
    company_external_id = _coerce_id(props.get("associatedcompanyid"))
    additional_emails = _split_email_list(props.get("hs_additional_emails"))
    phone = _coerce_str(props.get("phone"))
    mobile_phone = _coerce_str(props.get("mobilephone"))
    fax = _coerce_str(props.get("fax"))
    website = _coerce_str(props.get("website"))
    owner_external_id = _coerce_str(props.get("hubspot_owner_id"))
    lifecycle_stage = _coerce_str(props.get("lifecyclestage"))
    lead_status = _coerce_str(props.get("hs_lead_status"))
    marketing_status = _coerce_str(props.get("hs_marketable_status"))
    address = _coerce_str(props.get("address"))
    city = _coerce_str(props.get("city"))
    state = _coerce_str(props.get("state"))
    postal_code = _coerce_str(props.get("zip"))
    country = _coerce_str(props.get("country"))
    timezone = _coerce_str(props.get("hs_timezone"))

    last_contacted_at = _parse_iso(props.get("notes_last_contacted"))
    last_activity_at = _parse_iso(props.get("notes_last_updated"))

    source_created_at = (
        _parse_iso(raw.get("createdAt"))
        or _parse_iso(props.get("createdate"))
        or _parse_iso(props.get("hs_createdate"))
    )
    source_modified_at = (
        _parse_iso(raw.get("updatedAt"))
        or _parse_iso(props.get("lastmodifieddate"))
        or _parse_iso(props.get("hs_lastmodifieddate"))
    )

    curated_properties: dict[str, Any] = {
        "lifecycle_stage": lifecycle_stage,
        "lead_status": lead_status,
        "marketing_status": marketing_status,
        "associatedcompanyid": company_external_id,
        "raw_properties": props,
    }

    return NormalizedHubSpotContact(
        external_id=external_id,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        job_title=job_title,
        company_name=company_name,
        company_external_id=company_external_id,
        email=email,
        additional_emails=additional_emails,
        phone=phone,
        mobile_phone=mobile_phone,
        fax=fax,
        website=website,
        owner_external_id=owner_external_id,
        lifecycle_stage=lifecycle_stage,
        lead_status=lead_status,
        marketing_status=marketing_status,
        address=address,
        city=city,
        state=state,
        postal_code=postal_code,
        country=country,
        timezone=timezone,
        last_contacted_at=last_contacted_at,
        last_activity_at=last_activity_at,
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


def _normalize_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def _compose_full_name(
    first: str | None,
    last: str | None,
    email_fallback: str | None,
) -> str | None:
    parts = [p for p in (first, last) if p]
    if parts:
        return " ".join(parts)
    return email_fallback


def _split_email_list(value: Any) -> tuple[str, ...]:
    """HubSpot returns `hs_additional_emails` as a semicolon-delimited string."""
    if not isinstance(value, str) or not value:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for raw in value.split(";"):
        cleaned = raw.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return tuple(out)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
