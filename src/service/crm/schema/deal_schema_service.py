"""Deal schema metadata service for UI, import, and agent consumers.

The service owns the backend contract behind ``GET /users/crm/schema``. Routes
should only select the entity and serialize the returned models; field ownership,
writeability, and validation semantics live here so non-UI callers can reuse
the same rules.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.contracts.crm.deal_schema import (
    CRMEntitySchemaResponse,
    CRMFieldSchemaItem,
    DealCustomColumnDefinition,
    deal_detail_field_definitions,
)
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)

_CUSTOM_FIELD_ORDER_OFFSET = 1000
_OPTION_VALIDATED_TYPES = {
    CRMCustomFieldDataType.SELECT.value,
    CRMCustomFieldDataType.MULTISELECT.value,
}
_SEARCHABLE_STANDARD_FIELDS = {
    "name",
    "company_name",
    "company_domain",
    "primary_contact_name",
    "primary_contact_email",
    "description",
}
_FILTERABLE_STANDARD_FIELDS = {
    "deal_status",
    "amount",
    "close_date",
    "expected_close_date",
    "owner_details",
}
_CURRENCY_SYMBOL_BY_CODE = {
    "USD": "$",
    "INR": "₹",
}
_CURRENCY_CODE_PATTERN = re.compile(r"\b[A-Z]{3}\b")
_CURRENCY_CODE_BY_SYMBOL = {
    "$": "USD",
    "₹": "INR",
}


class DealSchemaService:
    """Builds ordered deal field metadata for the current organisation."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Close the underlying org session owned by the FastAPI dependency."""
        await self.session.close()

    async def get_schema(self) -> CRMEntitySchemaResponse:
        """Return standard deal fields plus active custom fields in display order."""
        fields = self._standard_fields()
        custom_fields = await self._load_custom_fields()
        fields.extend(
            self._custom_field_item(field, index=index) for index, field in enumerate(custom_fields)
        )
        return CRMEntitySchemaResponse(entity=CRMCustomFieldEntity.DEAL.value, fields=fields)

    async def active_internal_custom_field_keys(self) -> list[str]:
        """Return active internal custom keys for deal-detail null filling."""
        fields = await self._load_internal_custom_fields()
        return [field.key for field in fields if field.key]

    async def deal_detail_custom_column_definitions(
        self,
        *,
        deal_currency_code: str | None,
    ) -> list[DealCustomColumnDefinition]:
        """Return the legacy deal-detail custom-column projection from schema data.

        Task 6 keeps `custom_column_definitions` for existing detail consumers,
        but the mapping lives here so it stays aligned with the canonical schema
        endpoint instead of drifting in the route handler.
        """
        fields = await self._load_internal_custom_fields()
        return [
            self._detail_custom_column_definition(
                field,
                deal_currency_code=deal_currency_code,
            )
            for field in fields
        ]

    async def _load_custom_fields(self) -> list[CRMCustomFieldDefinition]:
        """Load active deal custom fields from every source without making them writable."""
        stmt = (
            select(CRMCustomFieldDefinition)
            .where(
                col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
                col(CRMCustomFieldDefinition.is_deleted).is_(False),
                col(CRMCustomFieldDefinition.is_active).is_(True),
            )
            .order_by(
                col(CRMCustomFieldDefinition.is_predefined).desc(),
                col(CRMCustomFieldDefinition.display_order).asc().nulls_last(),
                col(CRMCustomFieldDefinition.created_at).asc(),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _load_internal_custom_fields(self) -> list[CRMCustomFieldDefinition]:
        """Load active internal deal custom fields in the same order as schema output."""
        stmt = (
            select(CRMCustomFieldDefinition)
            .where(
                col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
                col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
                col(CRMCustomFieldDefinition.is_deleted).is_(False),
                col(CRMCustomFieldDefinition.is_active).is_(True),
            )
            .order_by(
                col(CRMCustomFieldDefinition.is_predefined).desc(),
                col(CRMCustomFieldDefinition.display_order).asc().nulls_last(),
                col(CRMCustomFieldDefinition.created_at).asc(),
            )
        )
        result = await self.session.execute(stmt)
        return [
            field
            for field in result.scalars().all()
            if _enum_value(field.field_source) == CRMCustomFieldSource.INTERNAL.value
        ]

    def _standard_fields(self) -> list[CRMFieldSchemaItem]:
        return [
            CRMFieldSchemaItem(
                field_id=None,
                entity=CRMCustomFieldEntity.DEAL.value,
                key=field.key,
                label=field.label,
                type=field.data_type,
                order=field.order,
                section=field.section,
                source="standard",
                ownership="system",
                integration_id=None,
                provider_name=None,
                provider_field_key=None,
                required=False,
                read_only=not field.editable,
                nullable=True,
                editable=field.editable,
                sortable=False,
                filterable=field.key in _FILTERABLE_STANDARD_FIELDS,
                searchable=field.key in _SEARCHABLE_STANDARD_FIELDS,
                input_control=field.edit_control or field.filter_control,
                lifecycle_state="active",
                write_path=field.patch_field,
                write_policy=field.edit_policy,
                read_only_reason=field.locked_reason,
                internal_write=field.editable,
                internal_write_validation={"required": False, "option_membership": False},
            )
            for field in deal_detail_field_definitions()
        ]

    def _custom_field_item(
        self, field: CRMCustomFieldDefinition, *, index: int
    ) -> CRMFieldSchemaItem:
        source = _enum_value(field.field_source)
        data_type = _enum_value(field.data_type)
        is_internal = source == CRMCustomFieldSource.INTERNAL.value
        is_formula = data_type == CRMCustomFieldDataType.FORMULA.value
        editable = is_internal and not is_formula
        required = bool(field.required) and editable
        provider_name = field.provider_name if isinstance(field.provider_name, str) else None
        read_only_reason = self._custom_read_only_reason(
            source=source,
            is_formula=is_formula,
        )

        return CRMFieldSchemaItem(
            field_id=field.field_id,
            entity=_enum_value(field.entity_type),
            key=field.key,
            label=field.label,
            type=data_type,
            order=_CUSTOM_FIELD_ORDER_OFFSET + index,
            section="custom",
            source=source,
            ownership=provider_name or source,
            integration_id=field.integration_id,
            provider_name=provider_name,
            provider_field_key=field.provider_field_key,
            required=required,
            read_only=not editable,
            nullable=not required,
            editable=editable,
            sortable=False,
            filterable=False,
            searchable=False,
            input_control=None if is_formula else data_type,
            options=field.options if isinstance(field.options, dict) else {},
            validation=field.validation if isinstance(field.validation, dict) else {},
            lifecycle_state="active",
            write_path=f"custom_columns.{field.key}" if editable else None,
            write_policy=self._custom_write_policy(source=source, is_formula=is_formula),
            read_only_reason=read_only_reason,
            internal_write=editable,
            internal_write_validation={
                "required": required,
                "option_membership": editable and data_type in _OPTION_VALIDATED_TYPES,
            },
        )

    def _detail_custom_column_definition(
        self,
        field: CRMCustomFieldDefinition,
        *,
        deal_currency_code: str | None,
    ) -> DealCustomColumnDefinition:
        data_type = _enum_value(field.data_type)
        currency_code = None
        currency_symbol = None
        if data_type == CRMCustomFieldDataType.CURRENCY.value:
            currency_code = _custom_field_currency_code(
                field,
                default_currency_code=deal_currency_code,
            )
            currency_symbol = _currency_symbol_for(currency_code)
        is_formula = data_type == CRMCustomFieldDataType.FORMULA.value

        return DealCustomColumnDefinition(
            key=field.key,
            label=field.label,
            data_type=data_type,
            required=bool(field.required),
            options=field.options if isinstance(field.options, dict) else {},
            validation=field.validation if isinstance(field.validation, dict) else {},
            currency_code=currency_code,
            currency_symbol=currency_symbol,
            editable=not is_formula,
            patch_field=None if is_formula else f"custom_columns.{field.key}",
            edit_control=None if is_formula else data_type,
            edit_policy="formula" if is_formula else "custom_columns",
            locked_reason="Formula custom fields are read-only." if is_formula else None,
        )

    @staticmethod
    def _custom_write_policy(*, source: str, is_formula: bool) -> str:
        if source == CRMCustomFieldSource.PROVIDER.value:
            return "provider_read_only"
        if is_formula:
            return "formula"
        return "custom_columns"

    @staticmethod
    def _custom_read_only_reason(*, source: str, is_formula: bool) -> str | None:
        if source == CRMCustomFieldSource.PROVIDER.value:
            return "Provider-backed fields are not writable through internal CRM writes."
        if is_formula:
            return "Formula custom fields are read-only."
        return None


def _enum_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _normalize_currency_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not normalized:
        return None
    if normalized in _CURRENCY_SYMBOL_BY_CODE:
        return normalized
    if normalized in _CURRENCY_CODE_BY_SYMBOL:
        return _CURRENCY_CODE_BY_SYMBOL[normalized]

    code_match = _CURRENCY_CODE_PATTERN.search(normalized)
    if code_match:
        candidate = code_match.group(0)
        if candidate in _CURRENCY_SYMBOL_BY_CODE:
            return candidate
    return normalized


def _currency_symbol_for(value: str | None) -> str | None:
    code = _normalize_currency_code(value)
    if code is None:
        return None
    return _CURRENCY_SYMBOL_BY_CODE.get(code)


def _custom_field_currency_code(
    field: CRMCustomFieldDefinition,
    *,
    default_currency_code: str | None,
) -> str | None:
    options = field.options if isinstance(field.options, dict) else {}
    validation = field.validation if isinstance(field.validation, dict) else {}

    for source in (options, validation):
        raw = source.get("currency_code") or source.get("currency")
        normalized = _normalize_currency_code(raw) if isinstance(raw, str) else None
        if normalized:
            return normalized
    return default_currency_code
