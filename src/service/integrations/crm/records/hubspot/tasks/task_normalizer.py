from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityObjectType,
    CRMActivityStatus,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.tasks.task_types import (
    NormalizedHubSpotTask,
)

logger = get_logger(__name__)


def normalize_tasks(
    raw_tasks: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedHubSpotTask], int]:
    normalized: list[NormalizedHubSpotTask] = []
    skipped_malformed = 0
    for raw in raw_tasks:
        task = _normalize_one(raw)
        if task is None:
            skipped_malformed += 1
            continue
        normalized.append(task)
    return normalized, skipped_malformed


def _normalize_one(raw: object) -> NormalizedHubSpotTask | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict HubSpot task payload")
        return None

    external_id = _coerce_id(raw.get("id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed HubSpot task (no usable id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    properties = raw.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    deal_external_id = _first_association_id(raw, "deals")
    contact_external_id = _first_association_id(raw, "contacts")
    company_external_id = _first_association_id(raw, "companies")

    related_object_type, related_external_id = _resolve_related_object(
        deal_external_id=deal_external_id,
        contact_external_id=contact_external_id,
        company_external_id=company_external_id,
    )

    normalized_properties = dict(properties)
    normalized_properties["provider"] = "hubspot"
    normalized_properties["associated_deal_ids"] = _all_association_ids(raw, "deals")
    normalized_properties["associated_contact_ids"] = _all_association_ids(raw, "contacts")
    normalized_properties["associated_company_ids"] = _all_association_ids(raw, "companies")

    status = _normalize_status(properties.get("hs_task_status"))
    due_date = _parse_hubspot_datetime(properties.get("hs_timestamp"))
    completed_date = due_date if status == CRMActivityStatus.COMPLETED.value else None

    return NormalizedHubSpotTask(
        external_id=external_id,
        subject=_coerce_str(properties.get("hs_task_subject")),
        body=_coerce_str(properties.get("hs_task_body")),
        status=status,
        priority=None,
        activity_subtype="HUBSPOT_TASK",
        owner_external_id=_coerce_id(properties.get("hubspot_owner_id")),
        due_date=due_date,
        completed_date=completed_date,
        deal_external_id=deal_external_id,
        contact_external_id=contact_external_id,
        company_external_id=company_external_id,
        related_object_type=related_object_type,
        related_external_id=related_external_id,
        source_created_at=_parse_hubspot_datetime(properties.get("hs_createdate")),
        source_modified_at=_parse_hubspot_datetime(properties.get("hs_lastmodifieddate")),
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
    canonical = raw.upper()
    if canonical == "COMPLETED":
        return CRMActivityStatus.COMPLETED.value
    if canonical == "NOT_STARTED":
        return CRMActivityStatus.NOT_STARTED.value
    return CRMActivityStatus.NOT_STARTED.value


def _first_association_id(raw: dict[str, Any], key: str) -> str | None:
    for candidate in _all_association_ids(raw, key):
        return candidate
    return None


def _all_association_ids(raw: dict[str, Any], key: str) -> list[str]:
    associations = raw.get("associations")
    if not isinstance(associations, dict):
        return []
    bucket = associations.get(key)
    if not isinstance(bucket, dict):
        return []
    results = bucket.get("results")
    if not isinstance(results, list):
        return []

    ids: list[str] = []
    seen: set[str] = set()
    for row in results:
        if not isinstance(row, dict):
            continue
        candidate = _coerce_id(row.get("id"))
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        ids.append(candidate)
    return ids


def _coerce_id(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, int):
        return str(value)
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _parse_hubspot_datetime(value: Any) -> datetime | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            return datetime.fromtimestamp(int(cleaned) / 1000.0, tz=UTC)
        try:
            return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
