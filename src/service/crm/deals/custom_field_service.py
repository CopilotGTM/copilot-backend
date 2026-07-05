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

        candidate_values: list[tuple[str, list[str], Any]] = []
        lookup_keys: list[str] = []
        invalid_keys: list[str] = []
        normalized_seen: set[str] = set()
        duplicate_normalized_keys: set[str] = set()
        for key, value in custom_properties.items():
            normalized_key = self.normalize_custom_key(key)
            if not normalized_key:
                invalid_keys.append(str(key))
                continue
            if normalized_key in normalized_seen:
                duplicate_normalized_keys.add(normalized_key)
            normalized_seen.add(normalized_key)
            exact_key = self.normalize_custom_key(key, strip_custom_prefix=False)
            candidates = [exact_key] if exact_key else []
            if normalized_key not in candidates:
                candidates.append(normalized_key)
            candidate_values.append((str(key), candidates, value))
            lookup_keys.extend(candidates)

        if invalid_keys:
            raise DealWriteServiceError(
                f"Invalid custom field keys: {', '.join(invalid_keys)}",
                status_code=400,
            )

        if duplicate_normalized_keys:
            raise DealWriteServiceError(
                f"Duplicate custom field keys: {', '.join(sorted(duplicate_normalized_keys))}",
                status_code=400,
            )

        if not lookup_keys:
            return {}

        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.is_active).is_(True),
            col(CRMCustomFieldDefinition.key).in_(lookup_keys),
        )
        result = await self.session.execute(stmt)
        fields = list(result.scalars().all())
        field_by_key = {field.key: field for field in fields}

        resolved_values: dict[str, Any] = {}
        unknown_keys: list[str] = []
        duplicate_keys: set[str] = set()
        for raw_key, candidates, value in candidate_values:
            resolved_key = next(
                (candidate for candidate in candidates if candidate in field_by_key), None
            )
            if resolved_key is None:
                unknown_keys.append(candidates[-1] if candidates else raw_key)
                continue
            if resolved_key in resolved_values:
                duplicate_keys.add(resolved_key)
            resolved_values[resolved_key] = value

        if unknown_keys:
            raise DealWriteServiceError(
                f"Unknown custom fields: {', '.join(sorted(set(unknown_keys)))}",
                status_code=400,
            )

        if duplicate_keys:
            raise DealWriteServiceError(
                f"Duplicate custom field keys: {', '.join(sorted(duplicate_keys))}",
                status_code=400,
            )

        coerced: dict[str, Any] = {}
        for key, value in resolved_values.items():
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
            coerced_value = self.coerce_custom_value(key=key, value=value, data_type=data_type)
            if data_type in {CRMCustomFieldDataType.SELECT, CRMCustomFieldDataType.MULTISELECT}:
                self.validate_option_membership(key=key, value=coerced_value, field=field)
            coerced[key] = coerced_value

        return coerced

    async def validate_required_on_create(self, custom_properties: dict[str, Any]) -> None:
        """Reject deal creation when active internal required fields are absent."""
        missing_keys = await self._missing_required_keys(custom_properties)
        if missing_keys:
            raise DealWriteServiceError(
                f"Missing required custom fields: {', '.join(missing_keys)}",
                status_code=400,
            )

    async def validate_required_not_cleared(self, custom_properties: dict[str, Any]) -> None:
        """Reject updates that explicitly clear active internal required fields."""
        required_keys = {field.key for field in await self._load_required_internal_fields()}
        cleared_keys = sorted(
            key
            for key, value in custom_properties.items()
            if key in required_keys and self._is_missing_required_value(value)
        )
        if cleared_keys:
            raise DealWriteServiceError(
                f"Required custom fields cannot be cleared: {', '.join(cleared_keys)}",
                status_code=400,
            )

    async def validate_required_for_terminal_transition(self, properties: dict[str, Any]) -> None:
        """Reject closing a deal until all active internal required fields have values."""
        missing_keys = await self._missing_required_keys(properties)
        if missing_keys:
            raise DealWriteServiceError(
                f"Missing required custom fields: {', '.join(missing_keys)}",
                status_code=400,
            )

    async def _missing_required_keys(self, properties: dict[str, Any]) -> list[str]:
        required_fields = await self._load_required_internal_fields()
        return sorted(
            field.key
            for field in required_fields
            if self._is_missing_required_value(properties.get(field.key))
        )

    async def _load_required_internal_fields(self) -> list[CRMCustomFieldDefinition]:
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.required).is_(True),
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.is_active).is_(True),
        )
        result = await self.session.execute(stmt)
        fields = list(result.scalars().all())
        return [field for field in fields if self._enforces_required_field(field)]

    @staticmethod
    def _enforces_required_field(field: CRMCustomFieldDefinition) -> bool:
        source = (
            field.field_source.value
            if isinstance(field.field_source, CRMCustomFieldSource)
            else str(field.field_source)
        )
        raw_data_type = (
            field.data_type.value
            if isinstance(field.data_type, CRMCustomFieldDataType)
            else str(field.data_type)
        )
        return (
            bool(field.required)
            and bool(field.is_active)
            and not bool(field.is_deleted)
            and source == CRMCustomFieldSource.INTERNAL.value
            and raw_data_type != CRMCustomFieldDataType.FORMULA.value
        )

    @staticmethod
    def _is_missing_required_value(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple, set, dict)):
            return len(value) == 0
        return False

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
                return self.parse_integer(value)
            if data_type in {CRMCustomFieldDataType.NUMBER, CRMCustomFieldDataType.CURRENCY}:
                return self.parse_currency(value)
            if data_type == CRMCustomFieldDataType.PERCENTAGE:
                parsed_percentage = self.parse_currency(value)
                if parsed_percentage < 0 or parsed_percentage > 100:
                    raise ValueError("Percentage out of range")
                return parsed_percentage
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

    def validate_option_membership(
        self,
        *,
        key: str,
        value: Any,
        field: CRMCustomFieldDefinition,
    ) -> None:
        """Reject select values that are not currently configured as active options."""
        if self._is_missing_required_value(value):
            return

        field_options = field.options if isinstance(field.options, dict) else {}
        allowed_values = self._active_option_values(field_options)
        submitted_values = value if isinstance(value, list) else [value]
        if any(str(item) not in allowed_values for item in submitted_values):
            raise DealWriteServiceError(
                f"Invalid option for custom field '{key}'",
                status_code=400,
            )

    @staticmethod
    def _active_option_values(options: dict[str, Any]) -> set[str]:
        values = options.get("values")
        if not isinstance(values, list):
            return set()

        active_values: set[str] = set()
        for item in values:
            if isinstance(item, dict):
                if item.get("active") is False or item.get("is_active") is False:
                    continue
                option_value = item.get("value")
            else:
                option_value = item

            if option_value is not None:
                active_values.add(str(option_value).strip())
        return active_values

    @staticmethod
    def parse_boolean(value: Any) -> bool:
        """Parse boolean custom fields without accepting arbitrary non-zero numbers."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if value not in {0, 1}:
                raise ValueError("Invalid boolean")
            return bool(value)
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
        raise ValueError("Invalid boolean")

    @staticmethod
    def parse_integer(value: Any) -> int:
        """Parse integer custom fields while rejecting bools and fractional values."""
        if isinstance(value, bool):
            raise ValueError("Invalid integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("Invalid integer")
            return int(value)
        text = str(value).strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError("Invalid integer")
        return int(text)

    @staticmethod
    def parse_date(value: Any) -> str:
        """Store date custom fields as ISO dates regardless of datetime input."""
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(str(value).strip()).isoformat()

    @staticmethod
    def parse_datetime(value: Any) -> str:
        """Store datetime custom fields as timezone-aware ISO strings."""
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()

    @staticmethod
    def parse_currency(value: Any) -> float:
        """Parse numeric and currency-like strings while rejecting booleans."""
        if isinstance(value, bool):
            raise ValueError("Invalid number")
        if isinstance(value, (int, float)):
            return float(value)
        text = re.sub(r"[^0-9.\-]", "", str(value))
        return float(text)

    @staticmethod
    def normalize_custom_key(value: Any, *, strip_custom_prefix: bool = True) -> str:
        """Normalize public custom field keys to the internal stored key format."""
        normalized = str(value).strip().lower()
        if strip_custom_prefix and normalized.startswith("custom_"):
            normalized = normalized[len("custom_") :]
        normalized = normalized.replace("-", "_").replace(" ", "_")
        normalized = _CUSTOM_KEY_INVALID_RE.sub("", normalized)
        return normalized.strip("_")
