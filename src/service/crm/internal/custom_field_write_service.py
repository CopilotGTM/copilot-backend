"""UI-driven write service for CRM custom field (custom column) definitions.

This service is intentionally separate from ``CustomFieldService`` (which lives
next door and was built around the bulk-CSV-import flow). The bulk-import path
is permissive — it auto-creates definitions for unknown CSV columns and is
designed to be retried many times during mapping QA. The settings-page path
this service powers is the opposite: every action is initiated by a human
clicking a button, so the rules are strict and the error messages are explicit.

Key design decisions:

* **Renames don't touch deals.** Deal values for a custom column live inside
  ``CRMDeal.properties`` JSONB and are keyed by the column's immutable ``key``
  (the slug, e.g. ``priority``). The ``label`` is purely a display name read
  from this definition row at render time, so renaming the label automatically
  shows up everywhere on the next refetch with no snapshot propagation.

* **Delete is blocked by in-use data.** A column cannot be soft-deleted while
  any non-deleted deal still has the key present in its ``properties``. This
  is enforced via the PostgreSQL JSONB ``?`` operator. There is no migration
  path — callers are expected to clear the column's value from those deals
  before retrying the delete.

* **Immutable fields are immutable at the service layer too.** ``key``,
  ``data_type``, ``entity_type`` and ``is_predefined`` cannot be changed after
  creation. The ``UpdateCustomFieldRequest`` schema already strips them at
  the HTTP boundary; this service doesn't even read them out of the payload,
  so a future caller bypassing the schema still cannot mutate them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_custom_field_models import (
    CreateCustomFieldRequest,
    ReorderCustomFieldsRequest,
    UpdateCustomFieldRequest,
)
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.leads import CRMLead
from src.infra.utils.logger import get_logger
from src.service.crm.mutations.contracts import CRMMutationContext, CRMMutationRecordCommand
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.json_safe import changed_field

logger = get_logger(__name__)

# Allowed key characters: lowercase letters, digits, underscores, starting with a letter.
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9_]+")

# Formula DSL ops the validator recognises. Mirrors the import-side service so
# that the two paths accept the same formula syntax — keeping them in sync
# avoids surprising users who created a formula via CSV import and want to
# edit it from the UI.
_SUPPORTED_FORMULA_OPS = frozenset({"add", "sub", "mul", "div", "concat", "coalesce", "if"})
_CUSTOM_FIELD_AUDIT_FIELDS = (
    "label",
    "required",
    "is_active",
    "display_order",
    "description",
    "options",
    "validation",
    "formula",
)


def _normalise_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_timestamp(left: datetime, right: datetime) -> bool:
    return _normalise_timestamp(left) == _normalise_timestamp(right)


class CustomFieldWriteServiceError(Exception):
    """Service-layer error with an HTTP status code attached for the handler."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CustomFieldWriteService:
    """UI-facing CRUD operations for CRM custom column definitions.

    All queries are scoped to the constructor's ``organisation_id``. The
    service owns its session — each public write method commits at the end
    (matching the pipeline write service pattern). Read methods are
    side-effect-free and never commit.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._mutation_events = CRMMutationEventService(session, organisation_id)

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("CustomFieldWriteService close exception", exc_info=True)

    # ----------------------------------------------------------------- reads

    async def list_fields(
        self,
        *,
        entity: CRMCustomFieldEntity = CRMCustomFieldEntity.DEAL,
        include_inactive: bool = False,
    ) -> list[CRMCustomFieldDefinition]:
        """Return non-deleted custom column definitions for the given entity.

        Predefined system fields sort first, then user fields by ``display_order``
        ascending (nulls last), then by creation time. ``include_inactive`` is
        used by the settings page to show archived columns alongside active ones.
        """
        stmt = (
            select(CRMCustomFieldDefinition)
            .where(
                col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == entity.value,
                col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
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

    async def get_field(self, field_id: UUID) -> CRMCustomFieldDefinition:
        """Load a single non-deleted definition by id, scoped to this org."""
        return await self._load_field(field_id)

    async def count_deal_usage_by_field_key(self, keys: list[str]) -> dict[str, int]:
        if not keys:
            return {}

        stmt = (
            select(CRMCustomFieldDefinition.key, func.count(CRMDeal.deal_id))
            .select_from(CRMCustomFieldDefinition)
            .outerjoin(
                CRMDeal,
                and_(
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.is_deleted).is_(False),
                    col(CRMDeal.properties).op("?")(col(CRMCustomFieldDefinition.key)),
                ),
            )
            .where(
                col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
                col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
                col(CRMCustomFieldDefinition.is_deleted).is_(False),
                col(CRMCustomFieldDefinition.key).in_(keys),
            )
            .group_by(CRMCustomFieldDefinition.key)
        )
        result = await self.session.execute(stmt)
        return {key: int(count or 0) for key, count in result.all()}

    # ---------------------------------------------------------------- writes

    async def create_field(self, request: CreateCustomFieldRequest) -> CRMCustomFieldDefinition:
        """Create a new custom column definition.

        Reject duplicate keys with 409, validate type-specific rules (SELECT
        needs options, FORMULA needs a formula, non-FORMULA must NOT carry a
        formula), then insert and commit.
        """
        entity = request.entity
        key = self._normalize_key(request.key)
        label = self._normalize_label(request.label)
        data_type = request.data_type

        if await self._key_exists(entity, key):
            raise CustomFieldWriteServiceError(
                f"A custom column with key '{key}' already exists",
                status_code=409,
            )

        options = request.options or {}
        validation = request.validation or {}
        formula = request.formula

        await self._validate_definition(
            entity=entity,
            data_type=data_type,
            options=options,
            formula=formula,
            current_key=key,
        )

        # is_predefined is a server-managed flag — admins seed predefined fields
        # via DB scripts only. Force it to False here regardless of caller input.
        field = CRMCustomFieldDefinition(
            organisation_id=self.organisation_id,
            entity_type=entity,
            key=key,
            label=label,
            data_type=data_type,
            required=request.required,
            is_predefined=False,
            is_active=True,
            display_order=request.display_order,
            description=request.description,
            options=options,
            validation=validation,
            formula=formula,
        )
        self.session.add(field)
        await self.session.flush()
        await self._record_settings_event(
            entity_id=field.field_id,
            operation="custom_field.create",
            event_type="crm.settings.custom_field.created",
            changed_fields=self._created_field_changes(field),
            summary=f"Created custom column {field.key}",
        )
        await self.session.commit()
        await self.session.refresh(field)
        return field

    async def update_field(
        self, field_id: UUID, request: UpdateCustomFieldRequest
    ) -> CRMCustomFieldDefinition:
        """Update mutable fields on an existing custom column.

        Renaming the ``label`` does NOT touch any deal rows — values are stored
        on each deal under the immutable ``key`` and the label is read from
        this row at display time.

        ``key``, ``data_type``, ``entity_type`` and ``is_predefined`` are
        immutable. The ``UpdateCustomFieldRequest`` schema doesn't expose them,
        and this method doesn't read them, so they remain unchanged regardless
        of what an upstream caller might try.
        """
        field = await self._load_field(field_id)
        if request.if_updated_at is None:
            raise CustomFieldWriteServiceError("if_updated_at is required", status_code=400)
        if not _same_timestamp(field.updated_at, request.if_updated_at):
            raise CustomFieldWriteServiceError(
                "Custom column changed since it was loaded. Refresh and retry.",
                status_code=409,
            )

        before = self._field_snapshot(field)

        # Only mutate fields the caller actually sent — exclude_unset matters
        # so that None values from the schema don't blank out existing data.
        payload = request.model_dump(exclude_unset=True, exclude={"if_updated_at"})

        if "label" in payload:
            field.label = self._normalize_label(payload["label"])
        if "required" in payload and payload["required"] is not None:
            field.required = bool(payload["required"])
        if "is_active" in payload and payload["is_active"] is not None:
            field.is_active = bool(payload["is_active"])
        if "display_order" in payload:
            field.display_order = payload["display_order"]
        if "description" in payload:
            field.description = payload["description"]
        if "options" in payload and payload["options"] is not None:
            field.options = payload["options"]
        if "validation" in payload and payload["validation"] is not None:
            field.validation = payload["validation"]
        if "formula" in payload:
            field.formula = payload["formula"]

        # Re-run validation with the updated state so that, e.g., changing the
        # SELECT options to an empty list is rejected before commit.
        await self._validate_definition(
            entity=CRMCustomFieldEntity(field.entity_type),
            data_type=CRMCustomFieldDataType(field.data_type),
            options=field.options or {},
            formula=field.formula,
            current_key=field.key,
        )

        field.updated_at = utcnow()
        self.session.add(field)
        await self.session.flush()
        await self._record_settings_event(
            entity_id=field.field_id,
            operation="custom_field.update",
            event_type="crm.settings.custom_field.updated",
            changed_fields=self._diff_snapshot(before, self._field_snapshot(field)),
            summary=f"Updated custom column {field.key}",
        )
        await self.session.commit()
        await self.session.refresh(field)
        return field

    async def reorder_fields(
        self, request: ReorderCustomFieldsRequest
    ) -> list[CRMCustomFieldDefinition]:
        """Save custom-column order for one entity as a complete-list write."""
        if len(set(request.field_ids)) != len(request.field_ids):
            raise CustomFieldWriteServiceError(
                "Duplicate custom column ids are not allowed",
                status_code=400,
            )

        fields = await self._load_reorderable_fields(request.entity, for_update=True)
        fields_by_id = {field.field_id: field for field in fields}
        if set(request.field_ids) != set(fields_by_id):
            raise CustomFieldWriteServiceError(
                "Custom column reorder must include every non-deleted custom column "
                "for this entity.",
                status_code=409,
            )

        request_version = _normalise_timestamp(request.if_updated_at)
        if any(_normalise_timestamp(field.updated_at) > request_version for field in fields):
            raise CustomFieldWriteServiceError(
                "Custom columns changed since they were loaded. Refresh and retry.",
                status_code=409,
            )

        now = utcnow()
        for display_order, field_id in enumerate(request.field_ids, start=1):
            field = fields_by_id[field_id]
            before_order = field.display_order
            field.display_order = display_order
            field.updated_at = now
            if change := changed_field(before_order, display_order):
                await self._record_settings_event(
                    entity_id=field.field_id,
                    operation="custom_field.reorder",
                    event_type="crm.settings.custom_field.reordered",
                    changed_fields={"display_order": change},
                    summary=f"Reordered custom column {getattr(field, 'key', field.field_id)}",
                )

        await self.session.flush()
        await self.session.commit()
        fields = await self._load_reorderable_fields(request.entity)
        return sorted(fields, key=lambda field: field.display_order or 0)

    async def delete_field(self, field_id: UUID) -> None:
        """Soft-delete a custom column. Blocked if any deal currently uses it.

        "In use" means: at least one non-deleted deal in this organisation has
        the field's ``key`` present in its ``crm_deals.properties`` JSONB. The
        check uses the PostgreSQL ``?`` operator on the JSONB column.
        """
        field = await self._load_field(field_id)

        if field.is_predefined:
            raise CustomFieldWriteServiceError(
                "Predefined custom columns cannot be deleted.",
                status_code=409,
            )

        if CRMCustomFieldEntity(field.entity_type) == CRMCustomFieldEntity.DEAL:
            deal_count = await self._count_deals_using_field(field.key)
            if deal_count > 0:
                raise CustomFieldWriteServiceError(
                    f"Cannot delete custom column '{field.key}': "
                    f"{deal_count} deal(s) currently use this column. "
                    "Remove the column data from those deals first.",
                    status_code=409,
                )

        before = {"is_active": field.is_active, "is_deleted": getattr(field, "is_deleted", False)}
        field.is_deleted = True
        field.is_active = False
        self.session.add(field)
        await self.session.flush()
        await self._record_settings_event(
            entity_id=field.field_id,
            operation="custom_field.delete",
            event_type="crm.settings.custom_field.deleted",
            changed_fields=self._diff_snapshot(
                before,
                {"is_active": field.is_active, "is_deleted": field.is_deleted},
            ),
            summary=f"Deleted custom column {field.key}",
        )
        await self.session.commit()

    async def restore_field(
        self, field_id: UUID, *, if_updated_at: datetime
    ) -> CRMCustomFieldDefinition:
        field = await self._load_field(field_id)
        if not _same_timestamp(field.updated_at, if_updated_at):
            raise CustomFieldWriteServiceError(
                "Custom column changed since it was loaded. Refresh and retry.",
                status_code=409,
            )
        if field.is_active:
            raise CustomFieldWriteServiceError("Custom column is already active.", status_code=409)

        before = {"is_active": field.is_active}
        field.is_active = True
        await self._validate_definition(
            entity=CRMCustomFieldEntity(field.entity_type),
            data_type=CRMCustomFieldDataType(field.data_type),
            options=field.options or {},
            formula=field.formula,
            current_key=field.key,
        )
        field.updated_at = utcnow()
        self.session.add(field)
        await self.session.flush()
        await self._record_settings_event(
            entity_id=field.field_id,
            operation="custom_field.restore",
            event_type="crm.settings.custom_field.restored",
            changed_fields=self._diff_snapshot(before, {"is_active": field.is_active}),
            summary=f"Restored custom column {field.key}",
        )
        await self.session.commit()
        await self.session.refresh(field)
        return field

    async def clear_field_values(self, field_id: UUID) -> int:
        field = await self._load_field(field_id)
        if field.is_predefined:
            raise CustomFieldWriteServiceError(
                "Predefined custom columns cannot be cleared.",
                status_code=409,
            )
        if field.is_active:
            raise CustomFieldWriteServiceError(
                "Archive the custom column before clearing its values.",
                status_code=409,
            )

        entity = CRMCustomFieldEntity(field.entity_type)
        if entity == CRMCustomFieldEntity.LEAD:
            usage_count = await self._count_leads_using_field(field.key)
            model = CRMLead
        else:
            usage_count = await self._count_deals_using_field(field.key)
            model = CRMDeal

        if usage_count == 0:
            return 0

        stmt = (
            update(model)
            .where(
                col(model.organisation_id) == self.organisation_id,
                col(model.is_deleted).is_(False),
                col(model.properties).op("?")(field.key),
            )
            .values(properties=col(model.properties).op("-")(field.key))
        )
        await self.session.execute(stmt)
        await self.session.commit()
        return usage_count

    async def validate_formula(
        self, *, entity: CRMCustomFieldEntity, formula: dict[str, Any]
    ) -> list[str]:
        """Validate a formula expression and return its sorted dependency keys.

        Used by the settings UI to give live feedback while editing a formula
        field, before the user commits the create/update. Does not write.
        """
        dependencies = await self._validate_formula(
            entity=entity, formula=formula, current_key=None
        )
        return sorted(dependencies)

    # --------------------------------------------------------------- helpers

    async def _record_settings_event(
        self,
        *,
        entity_id: UUID,
        operation: str,
        event_type: str,
        changed_fields: dict[str, dict[str, Any]],
        summary: str,
    ) -> None:
        await self._mutation_events.record_event(
            CRMMutationRecordCommand(
                entity_type="custom_field",
                entity_id=entity_id,
                event_type=event_type,
                operation=operation,
                changed_fields=changed_fields,
                semantic_event_types=[event_type],
                context=CRMMutationContext(source="api", actor_user_id=self.user_id),
                summary=summary,
            )
        )

    @staticmethod
    def _created_field_changes(field: CRMCustomFieldDefinition) -> dict[str, dict[str, Any]]:
        return {
            "key": {"old": None, "new": field.key},
            "entity_type": {"old": None, "new": field.entity_type},
            "data_type": {"old": None, "new": field.data_type},
            **{
                name: {"old": None, "new": getattr(field, name)}
                for name in _CUSTOM_FIELD_AUDIT_FIELDS
            },
        }

    @staticmethod
    def _field_snapshot(field: CRMCustomFieldDefinition) -> dict[str, Any]:
        return {name: getattr(field, name) for name in _CUSTOM_FIELD_AUDIT_FIELDS}

    @staticmethod
    def _diff_snapshot(
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        return {
            name: change
            for name, old in before.items()
            if (change := changed_field(old, after.get(name))) is not None
        }

    async def _load_field(self, field_id: UUID) -> CRMCustomFieldDefinition:
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.field_id) == field_id,
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        field = result.scalar_one_or_none()
        if not field:
            raise CustomFieldWriteServiceError("Custom column not found", status_code=404)
        return field

    async def _load_reorderable_fields(
        self, entity: CRMCustomFieldEntity, *, for_update: bool = False
    ) -> list[CRMCustomFieldDefinition]:
        stmt = (
            select(CRMCustomFieldDefinition)
            .where(
                col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == entity.value,
                col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
                col(CRMCustomFieldDefinition.is_deleted).is_(False),
            )
            .order_by(
                col(CRMCustomFieldDefinition.display_order).asc().nulls_last(),
                col(CRMCustomFieldDefinition.created_at).asc(),
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _key_exists(self, entity: CRMCustomFieldEntity, key: str) -> bool:
        stmt = select(col(CRMCustomFieldDefinition.field_id)).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == entity.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.key) == key,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first() is not None

    async def _count_deals_using_field(self, key: str) -> int:
        # `properties ? :key` is the JSONB top-level key-existence test. The
        # WHERE filters by organisation_id first (btree index), so this stays
        # cheap even without a GIN index on properties.
        # Wrapping with col() exposes the SQLAlchemy column element so we can
        # call .op() — the bare class attribute is typed as dict[str, Any].
        stmt = select(func.count()).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.is_deleted).is_(False),
            col(CRMDeal.properties).op("?")(key),
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def _count_leads_using_field(self, key: str) -> int:
        stmt = select(func.count()).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.is_deleted).is_(False),
            col(CRMLead.properties).op("?")(key),
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

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
                raise CustomFieldWriteServiceError(
                    "Select and multiselect columns require a non-empty options.values list",
                    status_code=400,
                )
            self._validate_option_values(values)

        if data_type != CRMCustomFieldDataType.FORMULA and formula is not None:
            raise CustomFieldWriteServiceError(
                "Formula payload is only allowed when data_type='formula'",
                status_code=400,
            )

        if data_type == CRMCustomFieldDataType.FORMULA:
            if formula is None:
                raise CustomFieldWriteServiceError(
                    "Formula columns require a formula payload",
                    status_code=400,
                )
            await self._validate_formula(entity=entity, formula=formula, current_key=current_key)

    def _validate_option_values(self, values: list[Any]) -> None:
        normalized_values: list[str] = []
        for item in values:
            value = item.get("value") if isinstance(item, dict) else item
            normalized = "" if value is None else str(value).strip()
            if not normalized:
                raise CustomFieldWriteServiceError(
                    "Select and multiselect option values cannot be empty",
                    status_code=400,
                )
            normalized_values.append(normalized)

        if len(set(normalized_values)) != len(normalized_values):
            raise CustomFieldWriteServiceError(
                "Select and multiselect option values must be unique",
                status_code=400,
            )

    async def _validate_formula(
        self,
        *,
        entity: CRMCustomFieldEntity,
        formula: dict[str, Any],
        current_key: str | None,
    ) -> set[str]:
        if not isinstance(formula, dict):
            raise CustomFieldWriteServiceError("Formula must be a JSON object", status_code=400)

        dependencies: set[str] = set()
        self._walk_formula(formula, dependencies)

        if current_key and current_key in dependencies:
            raise CustomFieldWriteServiceError(
                "Formula cannot reference its own column key",
                status_code=400,
            )

        # Resolve dependencies against the org's currently-active fields. The
        # current key is implicitly known (it's being created/updated).
        stmt = select(col(CRMCustomFieldDefinition.key)).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.entity_type) == entity.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.INTERNAL.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
            col(CRMCustomFieldDefinition.is_active).is_(True),
        )
        result = await self.session.execute(stmt)
        known_keys = {row[0] for row in result.all()}
        if current_key:
            known_keys.add(current_key)

        unknown = sorted(dependencies - known_keys)
        if unknown:
            raise CustomFieldWriteServiceError(
                f"Formula references unknown columns: {', '.join(unknown)}",
                status_code=400,
            )

        return dependencies

    def _walk_formula(self, node: Any, dependencies: set[str]) -> None:
        if isinstance(node, (str, int, float, bool)) or node is None:
            return

        if not isinstance(node, dict):
            raise CustomFieldWriteServiceError(
                "Formula nodes must be JSON objects", status_code=400
            )

        if "field" in node:
            dependencies.add(self._normalize_key(node.get("field")))
            return

        if "value" in node:
            return

        op = node.get("op")
        args = node.get("args")

        if op not in _SUPPORTED_FORMULA_OPS:
            raise CustomFieldWriteServiceError(
                f"Unsupported formula op '{op}'",
                status_code=400,
            )
        if not isinstance(args, list):
            raise CustomFieldWriteServiceError("Formula op args must be a list", status_code=400)

        if op == "if" and len(args) != 3:
            raise CustomFieldWriteServiceError(
                "Formula 'if' expects exactly 3 args", status_code=400
            )
        if op != "if" and len(args) < 1:
            raise CustomFieldWriteServiceError(
                f"Formula '{op}' expects at least one arg",
                status_code=400,
            )

        for arg in args:
            self._walk_formula(arg, dependencies)

    @staticmethod
    def _normalize_key(raw_key: Any) -> str:
        if not isinstance(raw_key, str):
            raise CustomFieldWriteServiceError("Column key must be a string", status_code=400)

        # Strip the CSV-import convention `custom_` prefix if a UI caller
        # accidentally sent it (e.g., copy-pasted from an import mapping).
        key = raw_key.strip().lower()
        if key.startswith("custom_"):
            key = key[len("custom_") :]
        key = key.replace("-", "_").replace(" ", "_")
        key = _KEY_NORMALIZE_RE.sub("_", key)
        key = re.sub(r"_+", "_", key).strip("_")

        if not key:
            raise CustomFieldWriteServiceError("Column key cannot be empty", status_code=400)
        if not _KEY_PATTERN.match(key):
            raise CustomFieldWriteServiceError(
                "Column key must start with a letter and contain only "
                "lowercase letters, digits, and underscores (max 63 chars)",
                status_code=400,
            )
        return key

    @staticmethod
    def _normalize_label(raw_label: Any) -> str:
        if not isinstance(raw_label, str):
            raise CustomFieldWriteServiceError("Column label must be a string", status_code=400)
        label = raw_label.strip()
        if not label:
            raise CustomFieldWriteServiceError("Column label cannot be empty", status_code=400)
        return label
