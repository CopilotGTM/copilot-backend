"""Normalize Salesforce Opportunity describe fields into registry rows."""

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


def map_salesforce_field_type(raw: Mapping[str, Any]) -> CRMCustomFieldDataType:
    field_type = str(raw.get("type") or "").lower()
    calculated = bool(raw.get("calculated"))
    formula = raw.get("calculatedFormula")

    if calculated and isinstance(formula, str) and formula.strip():
        return CRMCustomFieldDataType.FORMULA

    if field_type == "multipicklist":
        return CRMCustomFieldDataType.MULTISELECT
    if field_type in {"picklist", "combobox"}:
        return CRMCustomFieldDataType.SELECT
    if field_type == "currency":
        return CRMCustomFieldDataType.CURRENCY
    if field_type == "percent":
        return CRMCustomFieldDataType.PERCENTAGE
    if field_type in {"double", "decimal"}:
        return CRMCustomFieldDataType.NUMBER
    if field_type in {"int", "integer", "long"}:
        return CRMCustomFieldDataType.INTEGER
    if field_type == "boolean":
        return CRMCustomFieldDataType.BOOLEAN
    if field_type == "date":
        return CRMCustomFieldDataType.DATE
    if field_type == "datetime":
        return CRMCustomFieldDataType.DATETIME
    if field_type in {
        "string",
        "textarea",
        "phone",
        "url",
        "email",
        "reference",
        "id",
        "encryptedstring",
    }:
        return CRMCustomFieldDataType.TEXT

    logger.warning(
        "Unrecognized Salesforce field type, falling back to text",
        extra={"field": raw.get("name"), "type": raw.get("type")},
    )
    return CRMCustomFieldDataType.TEXT


def _parse_salesforce_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _provider_internal_key(name: str) -> str:
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
    if not isinstance(value, str) or not value:
        return None
    return value[:max_length]


def _normalize_options(raw_options: Any) -> list[NormalizedSchemaOption]:
    if not isinstance(raw_options, list):
        return []
    result: list[NormalizedSchemaOption] = []
    for option in raw_options:
        if not isinstance(option, Mapping):
            continue
        if not option.get("active", True):
            continue
        value = str(option.get("value") or "")
        if not value:
            continue
        label_raw = option.get("label")
        label = str(label_raw) if label_raw else value
        result.append(
            NormalizedSchemaOption(
                value=value,
                label=label,
                description=None,
                display_order=None,
                hidden=False,
            )
        )
    return result


def normalize_salesforce_field(
    raw: Mapping[str, Any],
    classification: ProviderCustomMatch,
) -> NormalizedSchemaField:
    field_name = classification.provider_field_key
    data_type = map_salesforce_field_type(raw)

    formula: dict[str, Any] | None = None
    if data_type == CRMCustomFieldDataType.FORMULA:
        formula = {"source": "salesforce", "expression": raw.get("calculatedFormula")}

    label_raw = raw.get("label")
    label = str(label_raw) if isinstance(label_raw, str) and label_raw else field_name

    return NormalizedSchemaField(
        provider_field_key=field_name,
        key=_provider_internal_key(field_name),
        label=label,
        description=_truncate_optional_string(
            raw.get("inlineHelpText"),
            max_length=_MAX_FIELD_DESCRIPTION_LENGTH,
        ),
        data_type=data_type,
        is_provider_custom=classification.is_provider_custom,
        display_order=None,
        options=_normalize_options(raw.get("picklistValues")),
        validation={},
        formula=formula,
        data_payload=dict(raw),
        source_modified_at=_parse_salesforce_timestamp(raw.get("lastModifiedDate")),
    )


def salesforce_options_payload(options: list[NormalizedSchemaOption]) -> dict[str, Any]:
    return serialize_options_payload(options, source="salesforce")
