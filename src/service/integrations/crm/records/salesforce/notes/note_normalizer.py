from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm import CRMActivityObjectType
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    NormalizedSalesforceNote,
)

logger = get_logger(__name__)


def normalize_notes(
    raw_notes: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedSalesforceNote], int]:
    grouped: dict[str, dict[str, Any]] = {}
    skipped_malformed = 0

    for raw in raw_notes:
        if not isinstance(raw, dict):
            logger.warning("Skipping non-dict Salesforce note payload")
            skipped_malformed += 1
            continue

        external_id = _coerce_str(raw.get("ContentDocumentId"))
        if external_id is None:
            logger.warning(
                "Skipping malformed Salesforce note (no usable ContentDocumentId)",
                extra={"raw_keys": list(raw.keys())},
            )
            skipped_malformed += 1
            continue

        entry = grouped.setdefault(
            external_id,
            {
                "subject": _coerce_str(raw.get("Title")),
                "body": _coerce_note_content(raw.get("Content")),
                "owner_external_id": _coerce_str(raw.get("OwnerId")),
                "source_created_at": _parse_salesforce_datetime(raw.get("CreatedDate")),
                "source_modified_at": _parse_salesforce_datetime(raw.get("LastModifiedDate")),
                "deal_external_id": None,
                "contact_external_id": None,
                "company_external_id": None,
                "raw_rows": [],
            },
        )
        entry["raw_rows"].append(raw)

        linked_entity_id = _coerce_str(raw.get("LinkedEntityId"))
        linked_type = _salesforce_object_type(linked_entity_id)
        if linked_type == "OPPORTUNITY":
            entry["deal_external_id"] = linked_entity_id
        elif linked_type == "CONTACT":
            entry["contact_external_id"] = linked_entity_id
        elif linked_type == "ACCOUNT":
            entry["company_external_id"] = linked_entity_id

    normalized: list[NormalizedSalesforceNote] = []
    for external_id, entry in grouped.items():
        deal_external_id = entry["deal_external_id"]
        contact_external_id = entry["contact_external_id"]
        company_external_id = entry["company_external_id"]
        related_object_type, related_external_id = _resolve_related_object(
            deal_external_id=deal_external_id,
            contact_external_id=contact_external_id,
            company_external_id=company_external_id,
        )
        normalized.append(
            NormalizedSalesforceNote(
                external_id=external_id,
                subject=entry["subject"],
                body=entry["body"],
                activity_subtype="SALESFORCE_NOTE",
                owner_external_id=entry["owner_external_id"],
                deal_external_id=deal_external_id,
                contact_external_id=contact_external_id,
                company_external_id=company_external_id,
                related_object_type=related_object_type,
                related_external_id=related_external_id,
                source_created_at=entry["source_created_at"],
                source_modified_at=entry["source_modified_at"],
                properties={"provider": "salesforce"},
                data_payload={"rows": entry["raw_rows"]},
            )
        )

    return normalized, skipped_malformed


def _resolve_related_object(
    *,
    deal_external_id: str | None,
    contact_external_id: str | None,
    company_external_id: str | None,
) -> tuple[str | None, str | None]:
    if deal_external_id is not None:
        return CRMActivityObjectType.DEAL.value, deal_external_id
    if contact_external_id is not None:
        return CRMActivityObjectType.CONTACT.value, contact_external_id
    if company_external_id is not None:
        return CRMActivityObjectType.COMPANY.value, company_external_id
    return None, None


def _salesforce_object_type(external_id: str | None) -> str | None:
    if external_id is None or len(external_id) < 3:
        return None
    prefix = external_id[:3]
    if prefix == "006":
        return "OPPORTUNITY"
    if prefix == "001":
        return "ACCOUNT"
    if prefix == "003":
        return "CONTACT"
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _coerce_note_content(value: Any) -> str | None:
    cleaned = _coerce_str(value)
    if cleaned is None:
        return None

    try:
        decoded = base64.b64decode(cleaned, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return cleaned

    stripped = decoded.strip()
    return stripped or None


def _parse_salesforce_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    for candidate in (
        cleaned.replace("Z", "+00:00"),
        f"{cleaned[:-5]}{cleaned[-5:-2]}:{cleaned[-2:]}" if len(cleaned) > 5 else cleaned,
    ):
        try:
            return datetime.fromisoformat(candidate).astimezone(UTC)
        except ValueError:
            continue
    return None
