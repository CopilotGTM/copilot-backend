"""Custom field validation and coercion for internal CRM deal writes."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.service.crm.deals.contracts import DealWriteServiceError

_CUSTOM_KEY_INVALID_RE = re.compile(r"[^a-z0-9_]+")


class DealCustomFieldWriteService:
    """Validates deal custom fields before callers write JSONB properties."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def validate_custom_properties(self, custom_properties: dict[str, Any]) -> dict[str, Any]:
        """Return normalized and type-coerced custom properties for deal writes."""
        if not isinstance(custom_properties, dict):
            raise DealWriteServiceError("custom_properties must be an object", status_code=400)

        normalized_map: dict[str, Any] = {}
        normalized_keys: list[str] = []
        for key, value in custom_properties.items():
            normalized_key = self.normalize_custom_key(key)
            normalized_map[normalized_key] = value
            normalized_keys.append(normalized_key)

        if not normalized_keys:
            return {}

        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.is_active).is_(True),
            col(CRMCustomFieldDefinition.key).in_(normalized_keys),
        )
        result = await self.session.execute(stmt)
        fields = list(result.scalars().all())
        field_by_key = {field.key: field for field in fields}

        unknown_keys = sorted(set(normalized_keys) - set(field_by_key))
        if unknown_keys:
            raise DealWriteServiceError(
                f"Unknown custom fields: {', '.join(unknown_keys)}",
                status_code=400,
            )

        coerced: dict[str, Any] = {}
        for key, value in normalized_map.items():
            field = field_by_key[key]
            raw_data_type = (
                field.data_type.value
                if isinstance(field.data_type, CRMCustomFieldDataType)
                else str(field.data_type)
            )
            data_type = CRMCustomFieldDataType(raw_data_type)
            if data_type == CRMCustomFieldDataType.FORMULA:
                raise DealWriteServiceError(
                    f"Formula custom field '{key}' is read-only",
                    status_code=400,
                )
            coerced[key] = self.coerce_custom_value(key=key, value=value, data_type=data_type)

        return coerced

    def coerce_custom_value(
        self,
        *,
        key: str,
        value: Any,
        data_type: CRMCustomFieldDataType,
    ) -> Any:
        """Coerce a caller-provided custom-field value to the stored representation."""
        if value is None:
            return None

        try:
            if data_type == CRMCustomFieldDataType.TEXT:
                return str(value).strip()
            if data_type == CRMCustomFieldDataType.INTEGER:
                return int(value)
            if data_type in {CRMCustomFieldDataType.NUMBER, CRMCustomFieldDataType.CURRENCY}:
                return self.parse_currency(value)
            if data_type == CRMCustomFieldDataType.BOOLEAN:
                return self.parse_boolean(value)
            if data_type == CRMCustomFieldDataType.DATE:
                return self.parse_date(value)
            if data_type == CRMCustomFieldDataType.DATETIME:
                return self.parse_datetime(value)
            if data_type == CRMCustomFieldDataType.SELECT:
                return str(value).strip()
            if data_type == CRMCustomFieldDataType.MULTISELECT:
                if isinstance(value, list):
                    return [str(item).strip() for item in value if str(item).strip()]
                return [str(value).strip()] if str(value).strip() else []
        except (TypeError, ValueError) as exc:
            raise DealWriteServiceError(
                f"Invalid value for custom field '{key}'",
                status_code=400,
            ) from exc

        return value

    @staticmethod
    def parse_boolean(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
        raise ValueError("Invalid boolean")

    @staticmethod
    def parse_date(value: Any) -> str:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(str(value).strip()).isoformat()

    @staticmethod
    def parse_datetime(value: Any) -> str:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()

    @staticmethod
    def parse_currency(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        text = re.sub(r"[^0-9.\-]", "", str(value))
        return float(text)

    @staticmethod
    def normalize_custom_key(value: Any) -> str:
        normalized = str(value).strip().lower().replace(" ", "_")
        normalized = _CUSTOM_KEY_INVALID_RE.sub("", normalized)
        return normalized.strip("_")
