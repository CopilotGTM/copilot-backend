from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityObjectType,
    CRMActivityPriority,
    CRMActivityStatus,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.tasks.task_types import (
    NormalizedSalesforceTask,
)

logger = get_logger(__name__)


def normalize_tasks(
    raw_tasks: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedSalesforceTask], int]:
    normalized: list[NormalizedSalesforceTask] = []
    skipped_malformed = 0
    for raw in raw_tasks:
        task = _normalize_one(raw)
        if task is None:
            skipped_malformed += 1
            continue
        normalized.append(task)
    return normalized, skipped_malformed


def _normalize_one(raw: object) -> NormalizedSalesforceTask | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict Salesforce task payload")
        return None

    external_id = _coerce_str(raw.get("Id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed Salesforce task (no usable Id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    what_id = _coerce_str(raw.get("WhatId"))
    who_id = _coerce_str(raw.get("WhoId"))
    account_id = _coerce_str(raw.get("AccountId"))
    deal_external_id = what_id if _salesforce_object_type(what_id) == "OPPORTUNITY" else None
    contact_external_id = who_id if _salesforce_object_type(who_id) == "CONTACT" else None
    company_external_id = account_id
    if company_external_id is None and _salesforce_object_type(what_id) == "ACCOUNT":
        company_external_id = what_id

    related_object_type, related_external_id = _resolve_related_object(
        deal_external_id=deal_external_id,
        contact_external_id=contact_external_id,
        company_external_id=company_external_id,
    )

    normalized_properties = {
        "provider": "salesforce",
        "raw_status": _coerce_str(raw.get("Status")),
        "raw_priority": _coerce_str(raw.get("Priority")),
        "salesforce_what_id": what_id,
        "salesforce_who_id": who_id,
        "salesforce_account_id": account_id,
    }

    return NormalizedSalesforceTask(
        external_id=external_id,
        subject=_coerce_str(raw.get("Subject")),
        body=_coerce_str(raw.get("Description")),
        status=_normalize_status(raw.get("Status")),
        priority=_normalize_priority(raw.get("Priority")),
        activity_subtype="SALESFORCE_TASK",
        owner_external_id=_coerce_str(raw.get("OwnerId")),
        due_date=_parse_salesforce_date(raw.get("ActivityDate")),
        completed_date=_parse_salesforce_datetime(raw.get("CompletedDateTime")),
        deal_external_id=deal_external_id,
        contact_external_id=contact_external_id,
        company_external_id=company_external_id,
        related_object_type=related_object_type,
        related_external_id=related_external_id,
        source_created_at=_parse_salesforce_datetime(raw.get("CreatedDate")),
        source_modified_at=_parse_salesforce_datetime(raw.get("LastModifiedDate")),
        properties=normalized_properties,
        data_payload=raw,
    )


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


def _normalize_status(value: Any) -> str | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    canonical = raw.strip().lower()
    if canonical in {"completed", "closed"}:
        return CRMActivityStatus.COMPLETED.value
    if canonical in {"in progress", "started", "waiting on someone else"}:
        return CRMActivityStatus.IN_PROGRESS.value
    if canonical in {"cancelled", "canceled"}:
        return CRMActivityStatus.CANCELLED.value
    return CRMActivityStatus.NOT_STARTED.value


def _normalize_priority(value: Any) -> str | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    canonical = raw.strip().lower()
    if canonical in {"high"}:
        return CRMActivityPriority.HIGH.value
    if canonical in {"normal", "medium"}:
        return CRMActivityPriority.MEDIUM.value
    if canonical in {"low"}:
        return CRMActivityPriority.LOW.value
    return CRMActivityPriority.NONE.value


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
    if prefix == "00Q":
        return "LEAD"
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


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


def _parse_salesforce_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return datetime.fromisoformat(cleaned).replace(tzinfo=UTC)
    except ValueError:
        return None
