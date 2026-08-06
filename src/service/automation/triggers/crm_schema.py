"""Merge tenant deal custom fields into the shared automation registry."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
)
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    AutomationRegistry,
    FieldType,
    RecordField,
)

_CUSTOM_TYPE_MAP = {
    CRMCustomFieldDataType.TEXT.value: FieldType.TEXT,
    CRMCustomFieldDataType.INTEGER.value: FieldType.INTEGER,
    CRMCustomFieldDataType.NUMBER.value: FieldType.DECIMAL,
    CRMCustomFieldDataType.DATE.value: FieldType.DATE,
    CRMCustomFieldDataType.DATETIME.value: FieldType.DATETIME,
    CRMCustomFieldDataType.CURRENCY.value: FieldType.DECIMAL,
    CRMCustomFieldDataType.PERCENTAGE.value: FieldType.DECIMAL,
    CRMCustomFieldDataType.BOOLEAN.value: FieldType.BOOLEAN,
    CRMCustomFieldDataType.SELECT.value: FieldType.ENUM,
    CRMCustomFieldDataType.MULTISELECT.value: FieldType.LIST,
}
_DEAL_LIFECYCLE_EVENTS = {
    "crm.deal.created",
    "crm.deal.updated",
    "crm.deal.deleted",
}


@dataclass(frozen=True)
class AutomationRegistrySnapshot:
    """Tenant registry plus option values rendered by the same schema snapshot."""

    registry: AutomationRegistry
    option_sets: dict[str, tuple[tuple[str, str], ...]]


class AutomationCRMRegistryService:
    """Build one request/activation-scoped registry from current CRM schema."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def snapshot(self) -> AutomationRegistrySnapshot:
        """Capture the crm schema state as an immutable snapshot for later validation."""
        from src.service.crm.schema.deal_schema_service import DealSchemaService

        schema = DealSchemaService(self.session, self.organisation_id)
        fields = await schema.active_custom_field_definitions()
        return merge_deal_custom_fields(fields)


def merge_deal_custom_fields(
    fields: list[CRMCustomFieldDefinition],
    *,
    base: AutomationRegistry = AUTOMATION_REGISTRY,
) -> AutomationRegistrySnapshot:
    """Return a registry whose deal lifecycle schema includes active custom fields."""

    record_fields: list[RecordField] = []
    option_sets: dict[str, tuple[tuple[str, str], ...]] = {}
    for field in fields:
        value_type = custom_field_type(field)
        if value_type is None:
            continue
        option_source = None
        if value_type in {FieldType.ENUM, FieldType.LIST}:
            option_source = f"custom_field:{field.field_id}"
            option_sets[option_source] = tuple(active_custom_field_options(field.options))
        record_fields.append(
            RecordField(
                key=f"custom_properties.{field.key}",
                label=field.label,
                value_type=value_type,
                description=field.description or "",
                exact=value_type == FieldType.ENUM,
                option_source=option_source,
                accessor=("custom_properties", field.key),
            )
        )

    events = [
        replace(
            event,
            record_fields=(*event.record_fields, *record_fields),
            allow_dynamic_record=False,
        )
        if event.event_key in _DEAL_LIFECYCLE_EVENTS
        else event
        for event in base.events.values()
    ]
    return AutomationRegistrySnapshot(
        registry=AutomationRegistry(events, base.triggers.values()),
        option_sets=option_sets,
    )


def active_custom_field_options(options: Any) -> list[tuple[str, str]]:
    """Return active stored values and labels from one custom select definition."""

    values = options.get("values") if isinstance(options, dict) else None
    if not isinstance(values, list):
        return []
    result: list[tuple[str, str]] = []
    for item in values:
        if isinstance(item, dict):
            if item.get("active") is False or item.get("is_active") is False:
                continue
            value = item.get("value")
            label = item.get("label", value)
        else:
            value = label = item
        if value is None or not str(value).strip():
            continue
        normalized = str(value).strip()
        result.append((normalized, str(label).strip() or normalized))
    return result


def custom_field_type(field: CRMCustomFieldDefinition) -> FieldType | None:
    """Apply the custom field type contract for this organisation-scoped module."""
    data_type = _value(field.data_type)
    if data_type != CRMCustomFieldDataType.FORMULA.value:
        return _CUSTOM_TYPE_MAP.get(data_type)
    formula = field.formula if isinstance(field.formula, dict) else {}
    validation = field.validation if isinstance(field.validation, dict) else {}
    declared = formula.get("result_type") or validation.get("result_type")
    return _CUSTOM_TYPE_MAP.get(str(declared)) if declared else None


def _value(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value)


__all__ = [
    "AutomationCRMRegistryService",
    "AutomationRegistrySnapshot",
    "active_custom_field_options",
    "custom_field_type",
    "merge_deal_custom_fields",
]
