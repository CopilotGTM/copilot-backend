"""Salesforce Contact -> NormalizedSalesforceContact."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.contacts.contact_types import (
    NormalizedSalesforceContact,
)

logger = get_logger(__name__)


def normalize_contacts(
    raw_contacts: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedSalesforceContact], int]:
    normalized: list[NormalizedSalesforceContact] = []
    skipped_malformed = 0
    for raw in raw_contacts:
        contact = _normalize_one(raw)
        if contact is None:
            skipped_malformed += 1
            continue
        normalized.append(contact)
    return normalized, skipped_malformed


def _normalize_one(raw: object) -> NormalizedSalesforceContact | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict Salesforce contact payload")
        return None

    external_id = _coerce_id(raw.get("Id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed Salesforce contact (no usable Id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    first_name = _coerce_str(raw.get("FirstName"))
    last_name = _coerce_str(raw.get("LastName"))
    email = normalize_email(raw.get("Email"))
    return NormalizedSalesforceContact(
        external_id=external_id,
        first_name=first_name,
        last_name=last_name,
        full_name=_coerce_str(raw.get("Name")) or _compose_full_name(first_name, last_name, email),
        job_title=_coerce_str(raw.get("Title")),
        company_name=None,
        company_external_id=_coerce_str(raw.get("AccountId")),
        email=email,
        additional_emails=(),
        phone=_coerce_str(raw.get("Phone")),
        mobile_phone=_coerce_str(raw.get("MobilePhone")),
        fax=_coerce_str(raw.get("Fax")),
        website=_coerce_str(raw.get("Website")),
        linkedin_url=_coerce_str(raw.get("LinkedIn_URL__c")),
        owner_external_id=_coerce_str(raw.get("OwnerId")),
        lifecycle_stage=None,
        lead_status=None,
        marketing_status=None,
        address=_coerce_str(raw.get("MailingStreet")),
        city=_coerce_str(raw.get("MailingCity")),
        state=_coerce_str(raw.get("MailingState")),
        postal_code=_coerce_str(raw.get("MailingPostalCode")),
        country=_coerce_str(raw.get("MailingCountry")),
        timezone=None,
        last_contacted_at=None,
        # Salesforce exposes Contact freshness as a date-only value. Preserve it
        # so the writer can compare provider and CopilotGTM activity consistently.
        last_activity_at=_parse_salesforce_timestamp(raw.get("LastActivityDate")),
        source_created_at=_parse_salesforce_timestamp(raw.get("CreatedDate")),
        source_modified_at=_parse_salesforce_timestamp(raw.get("LastModifiedDate")),
        properties={
            "salesforce_object": "Contact",
            "salesforce_department": _coerce_str(raw.get("Department")),
            "raw_fields": raw,
        },
        data_payload=raw,
    )


def normalize_email(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


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


def _compose_full_name(
    first_name: str | None,
    last_name: str | None,
    email_fallback: str | None,
) -> str | None:
    parts = [part for part in (first_name, last_name) if part]
    if parts:
        return " ".join(parts)
    return email_fallback


def _parse_salesforce_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value
    if len(cleaned) >= 5 and cleaned[-5] in {"+", "-"} and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
