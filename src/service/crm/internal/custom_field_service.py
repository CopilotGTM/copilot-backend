"""Service layer for CRM custom field definition management."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9_]+")

_SUPPORTED_FORMULA_OPS = {
    "add",
    "sub",
    "mul",
    "div",
    "concat",
    "coalesce",
    "if",
}


class CustomFieldServiceError(Exception):
    """Service error with status code for API responses."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CustomFieldService:
    """Organisation-scoped operations for custom field definitions."""

    # Start with an empty seed list to avoid silently forcing business fields.
    # We can seed defaults per org later without changing API contracts.
    _PREDEFINED_DEAL_FIELDS: tuple[dict[str, Any], ...] = ()

    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("CustomFieldService close failed", exc_info=True)

    async def list_fields(
        self, entity: CRMCustomFieldEntity, *, include_inactive: bool = False
    ) -> list[CRMCustomFieldDefinition]:
        await self._ensure_predefined_fields(entity)

        stmt = (
            select(CRMCustomFieldDefinition)
            .where(
                col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == entity.value,
                col(CRMCustomFieldDefinition.is_deleted).is_(False),
            )
            .order_by(
                col(CRMCustomFieldDefinition.is_predefined).desc(),
                col(CRMCustomFieldDefinition.display_order).asc().nulls_last(),
                col(CRMCustomFieldDefinition.created_at).asc(),
            )
        )
        if not include_inactive:
            stmt = stmt.where(col(CRMCustomFieldDefinition.is_active).is_(True))

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_field_map(
        self,
        entity: CRMCustomFieldEntity,
        keys: list[str],
        *,
        include_inactive: bool = False,
    ) -> dict[str, CRMCustomFieldDefinition]:
        """Return definitions by key for the provided entity + key set."""
        if not keys:
            return {}

        normalized_keys = {self._normalize_key(key) for key in keys}
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == entity.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.key).in_(list(normalized_keys)),
        )
        if not include_inactive:
            stmt = stmt.where(col(CRMCustomFieldDefinition.is_active).is_(True))

        result = await self.session.execute(stmt)
        return {field.key: field for field in result.scalars().all()}

    async def create_field(self, payload: dict[str, Any]) -> CRMCustomFieldDefinition:
        entity = CRMCustomFieldEntity(payload.get("entity", CRMCustomFieldEntity.DEAL))
        key = self._normalize_key(payload.get("key"))
        label = self._normalize_label(payload.get("label"))
        data_type = CRMCustomFieldDataType(payload.get("data_type", CRMCustomFieldDataType.TEXT))

        existing = await self._get_by_key(entity, key)
        if existing:
            raise CustomFieldServiceError(
                f"Custom field key '{key}' already exists for {entity.value}",
                status_code=409,
            )

        options = payload.get("options") or {}
        validation = payload.get("validation") or {}
        formula = payload.get("formula")

        await self._validate_definition(
            entity=entity,
            data_type=data_type,
            options=options,
            formula=formula,
            current_key=key,
        )

        field = CRMCustomFieldDefinition(
            organisation_id=self.organisation_id,
            entity_type=entity,
            key=key,
            label=label,
            data_type=data_type,
            required=bool(payload.get("required", False)),
            is_predefined=bool(payload.get("is_predefined", False)),
            is_active=bool(payload.get("is_active", True)),
            display_order=payload.get("display_order"),
            description=payload.get("description"),
            options=options,
            validation=validation,
            formula=formula,
        )
        self.session.add(field)
        await self.session.flush()
        return field

    async def update_field(
        self, field_id: UUID, payload: dict[str, Any]
    ) -> CRMCustomFieldDefinition:
        field = await self._get_by_id(field_id)

        if "label" in payload and payload["label"] is not None:
            field.label = self._normalize_label(payload["label"])
        if "required" in payload and payload["required"] is not None:
            field.required = bool(payload["required"])
        if "is_active" in payload and payload["is_active"] is not None:
            field.is_active = bool(payload["is_active"])
        if "display_order" in payload:
            field.display_order = payload.get("display_order")
        if "description" in payload:
            field.description = payload.get("description")
        if "options" in payload and payload["options"] is not None:
            field.options = payload["options"]
        if "validation" in payload and payload["validation"] is not None:
            field.validation = payload["validation"]
        if "formula" in payload:
            field.formula = payload.get("formula")

        await self._validate_definition(
            entity=CRMCustomFieldEntity(field.entity_type),
            data_type=CRMCustomFieldDataType(field.data_type),
            options=field.options or {},
            formula=field.formula,
            current_key=field.key,
        )

        self.session.add(field)
        await self.session.flush()
        return field

    async def archive_field(self, field_id: UUID) -> CRMCustomFieldDefinition:
        field = await self._get_by_id(field_id)

        if field.is_predefined:
            raise CustomFieldServiceError(
                "Predefined custom fields cannot be deleted",
                status_code=409,
            )

        field.is_deleted = True
        field.is_active = False
        self.session.add(field)
        await self.session.flush()
        return field

    async def validate_formula(
        self, entity: CRMCustomFieldEntity, formula: dict[str, Any]
    ) -> list[str]:
        dependencies = await self._validate_formula(
            entity=entity, formula=formula, current_key=None
        )
        return sorted(dependencies)

    async def _get_by_id(self, field_id: UUID) -> CRMCustomFieldDefinition:
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.field_id) == field_id,
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        field = result.scalar_one_or_none()
        if not field:
            raise CustomFieldServiceError("Custom field not found", status_code=404)
        return field

    async def _get_by_key(
        self, entity: CRMCustomFieldEntity, key: str
    ) -> CRMCustomFieldDefinition | None:
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == entity.value,
            col(CRMCustomFieldDefinition.key) == key,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _ensure_predefined_fields(self, entity: CRMCustomFieldEntity) -> None:
        if entity != CRMCustomFieldEntity.DEAL:
            return
        if not self._PREDEFINED_DEAL_FIELDS:
            return

        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
            col(CRMCustomFieldDefinition.is_predefined).is_(True),
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        existing_keys = {row.key for row in result.scalars().all()}

        created = False
        for item in self._PREDEFINED_DEAL_FIELDS:
            key = item["key"]
            if key in existing_keys:
                continue

            field = CRMCustomFieldDefinition(
                organisation_id=self.organisation_id,
                entity_type=CRMCustomFieldEntity.DEAL,
                key=key,
                label=item["label"],
                data_type=CRMCustomFieldDataType(item["data_type"]),
                required=False,
                is_predefined=True,
                is_active=True,
                display_order=item.get("display_order"),
                description=item.get("description"),
                options=item.get("options", {}),
                validation=item.get("validation", {}),
                formula=item.get("formula"),
            )
            self.session.add(field)
            created = True

        if created:
            await self.session.flush()
            await self.session.commit()

    async def _validate_definition(
        self,
        *,
        entity: CRMCustomFieldEntity,
        data_type: CRMCustomFieldDataType,
        options: dict[str, Any],
        formula: dict[str, Any] | None,
        current_key: str,
    ) -> None:
        if data_type in {CRMCustomFieldDataType.SELECT, CRMCustomFieldDataType.MULTISELECT}:
            values = options.get("values")
            if not isinstance(values, list) or not values:
                raise CustomFieldServiceError(
                    "Select and multiselect fields require options.values list",
                    status_code=400,
                )

        if data_type != CRMCustomFieldDataType.FORMULA and formula is not None:
            raise CustomFieldServiceError(
                "Formula payload is only allowed for data_type='formula'",
                status_code=400,
            )

        if data_type == CRMCustomFieldDataType.FORMULA:
            if formula is None:
                raise CustomFieldServiceError(
                    "Formula field requires formula payload",
                    status_code=400,
                )
            await self._validate_formula(entity=entity, formula=formula, current_key=current_key)

    async def _validate_formula(
        self,
        *,
        entity: CRMCustomFieldEntity,
        formula: dict[str, Any],
        current_key: str | None,
    ) -> set[str]:
        if not isinstance(formula, dict):
            raise CustomFieldServiceError("Formula must be a JSON object", status_code=400)

        dependencies: set[str] = set()
        self._walk_formula(formula, dependencies)

        if current_key and current_key in dependencies:
            raise CustomFieldServiceError(
                "Formula cannot reference its own field key",
                status_code=400,
            )

        stmt = select(col(CRMCustomFieldDefinition.key)).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == entity.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.is_active).is_(True),
        )
        result = await self.session.execute(stmt)
        known_keys = {row[0] for row in result.all()}
        if current_key:
            known_keys.add(current_key)

        unknown = sorted(dependencies - known_keys)
        if unknown:
            raise CustomFieldServiceError(
                f"Formula references unknown fields: {', '.join(unknown)}",
                status_code=400,
            )

        return dependencies

    def _walk_formula(self, node: Any, dependencies: set[str]) -> None:
        if isinstance(node, (str, int, float, bool)) or node is None:
            return

        if not isinstance(node, dict):
            raise CustomFieldServiceError("Formula nodes must be objects", status_code=400)

        if "field" in node:
            field_key = self._normalize_key(node.get("field"))
            dependencies.add(field_key)
            return

        if "value" in node:
            return

        op = node.get("op")
        args = node.get("args")

        if op not in _SUPPORTED_FORMULA_OPS:
            raise CustomFieldServiceError(
                f"Unsupported formula op '{op}'",
                status_code=400,
            )
        if not isinstance(args, list):
            raise CustomFieldServiceError("Formula op args must be a list", status_code=400)

        if op == "if" and len(args) != 3:
            raise CustomFieldServiceError("Formula 'if' expects exactly 3 args", status_code=400)
        if op != "if" and len(args) < 1:
            raise CustomFieldServiceError(
                f"Formula '{op}' expects at least one arg",
                status_code=400,
            )

        for arg in args:
            self._walk_formula(arg, dependencies)

    @staticmethod
    def _normalize_key(raw_key: Any) -> str:
        if not isinstance(raw_key, str):
            raise CustomFieldServiceError("Field key must be a string", status_code=400)

        key = raw_key.strip().lower()
        if key.startswith("custom_"):
            key = key[len("custom_") :]
        key = key.replace("-", "_").replace(" ", "_")
        key = _KEY_NORMALIZE_RE.sub("_", key)
        key = re.sub(r"_+", "_", key).strip("_")

        if not key:
            raise CustomFieldServiceError("Field key cannot be empty", status_code=400)
        if not re.match(r"^[a-z][a-z0-9_]{1,62}$", key):
            raise CustomFieldServiceError(
                "Field key must match ^[a-z][a-z0-9_]{1,62}$",
                status_code=400,
            )
        return key

    @staticmethod
    def _normalize_label(raw_label: Any) -> str:
        if not isinstance(raw_label, str):
            raise CustomFieldServiceError("Field label must be a string", status_code=400)
        label = raw_label.strip()
        if not label:
            raise CustomFieldServiceError("Field label cannot be empty", status_code=400)
        return label
