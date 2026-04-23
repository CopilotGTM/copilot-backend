"""Pure normalization of raw HubSpot deal properties into NormalizedSchemaField.

Only invoked for ProviderCustomMatch classifications — canonical matches are
discarded before this step. Produces exactly the shape the registry writer
persists.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.common.options import serialize_options_payload
from src.service.integrations.crm.schema.common.types import (
    NormalizedSchemaField,
    NormalizedSchemaOption,
    ProviderCustomMatch,
)

logger = get_logger(__name__)
_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9_]+")
_MAX_CUSTOM_FIELD_KEY_LENGTH = 63
_KEY_HASH_LENGTH = 8
_MAX_FIELD_DESCRIPTION_LENGTH = 5000


def map_hubspot_field_type(raw: Mapping[str, Any]) -> CRMCustomFieldDataType:
    """Convert HubSpot raw type hints into the internal enum used for rendering/storage."""

    raw_type = str(raw.get("type") or "").lower()
    field_type = str(raw.get("fieldType") or "").lower()
    display_hint = str(raw.get("numberDisplayHint") or "").lower()
    calculated = bool(raw.get("calculated"))
    formula = raw.get("calculationFormula")

    if calculated and isinstance(formula, str) and formula.strip():
        return CRMCustomFieldDataType.FORMULA

    if raw_type == "enumeration" and field_type in {"checkbox", "multiple_checkboxes"}:
        return CRMCustomFieldDataType.MULTISELECT
    if raw_type == "enumeration":
        return CRMCustomFieldDataType.SELECT

    if raw_type == "number" and display_hint == "currency":
        return CRMCustomFieldDataType.CURRENCY
    if raw_type == "number" and display_hint == "percentage":
        return CRMCustomFieldDataType.PERCENTAGE
    if raw_type == "number":
        return CRMCustomFieldDataType.NUMBER

    if raw_type == "bool":
        return CRMCustomFieldDataType.BOOLEAN
    if raw_type == "date":
        return CRMCustomFieldDataType.DATE
    if raw_type == "datetime":
        return CRMCustomFieldDataType.DATETIME
    if raw_type == "string":
        return CRMCustomFieldDataType.TEXT

    logger.warning(
        "Unrecognized HubSpot type, falling back to text",
        extra={
            "property": raw.get("name"),
            "type": raw.get("type"),
            "fieldType": raw.get("fieldType"),
        },
    )
    return CRMCustomFieldDataType.TEXT


def _parse_hubspot_timestamp(value: Any) -> datetime | None:
    """Parse HubSpot ISO8601 timestamps, or return `None` instead of failing sync."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_options(raw_options: Any) -> list[NormalizedSchemaOption]:
    """Drop malformed option rows and normalize each option payload to a stable schema."""

    if not isinstance(raw_options, list):
        return []
    result: list[NormalizedSchemaOption] = []
    for opt in raw_options:
        if not isinstance(opt, Mapping):
            continue
        value = str(opt.get("value") or "")
        if not value:
            continue
        label_raw = opt.get("label")
        label = str(label_raw) if label_raw else value
        description_raw = opt.get("description")
        description = description_raw if isinstance(description_raw, str) else None
        display_order = opt.get("displayOrder")
        if display_order is not None and not isinstance(display_order, int):
            display_order = None
        result.append(
            NormalizedSchemaOption(
                value=value,
                label=label,
                description=description,
                display_order=display_order,
                hidden=bool(opt.get("hidden", False)),
            )
        )
    return result


def _provider_internal_key(name: str) -> str:
    """Derive a DB-safe custom-field key from an arbitrary HubSpot property name."""

    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    key = _KEY_NORMALIZE_RE.sub("_", key)
    key = re.sub(r"_+", "_", key).strip("_")

    if not key:
        key = "field"
    if not key[0].isalpha():
        key = f"field_{key}"
    if len(key) <= _MAX_CUSTOM_FIELD_KEY_LENGTH:
        return key

    suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_KEY_HASH_LENGTH]
    prefix_length = _MAX_CUSTOM_FIELD_KEY_LENGTH - _KEY_HASH_LENGTH - 1
    prefix = key[:prefix_length].rstrip("_") or "field"
    return f"{prefix}_{suffix}"


def _truncate_optional_string(value: Any, *, max_length: int) -> str | None:
    """Limit optional string fields so we don’t exceed DB column constraints."""

    if not isinstance(value, str) or not value:
        return None
    return value[:max_length]


def normalize(
    raw: Mapping[str, Any],
    classification: ProviderCustomMatch,
) -> NormalizedSchemaField:
    """Build a normalized provider field from raw payload and classification context."""

    name = classification.provider_field_key
    data_type = map_hubspot_field_type(raw)

    formula: dict[str, Any] | None = None
    if data_type == CRMCustomFieldDataType.FORMULA:
        formula = {"source": "hubspot", "expression": raw.get("calculationFormula")}

    label_raw = raw.get("label")
    label = str(label_raw) if isinstance(label_raw, str) and label_raw else name

    description = _truncate_optional_string(
        raw.get("description"),
        max_length=_MAX_FIELD_DESCRIPTION_LENGTH,
    )

    display_order = raw.get("displayOrder")
    if display_order is not None and not isinstance(display_order, int):
        display_order = None

    return NormalizedSchemaField(
        provider_field_key=name,
        key=_provider_internal_key(name),
        label=label,
        description=description,
        data_type=data_type,
        is_provider_custom=classification.is_provider_custom,
        display_order=display_order,
        options=_normalize_options(raw.get("options")),
        validation={},
        formula=formula,
        data_payload=dict(raw),
        source_modified_at=_parse_hubspot_timestamp(raw.get("updatedAt"))
        or _parse_hubspot_timestamp(raw.get("createdAt")),
    )


def options_payload(options: list[NormalizedSchemaOption]) -> dict[str, Any]:
    """Shape options into the JSON payload persisted in `CRMCustomFieldDefinition.options`."""
    return serialize_options_payload(options, source="hubspot")
