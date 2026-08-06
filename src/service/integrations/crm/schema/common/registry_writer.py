"""Provider-backed persistence for CRM schema-sync registry rows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.infra.utils.logger import get_logger
from src.service.automation.triggers.crm_schema import custom_field_type
from src.service.automation.triggers.service import (
    AutomationTriggerSchemaReconciliationService,
)
from src.service.integrations.crm.schema.common.types import (
    NormalizedSchemaField,
    NormalizedSchemaOption,
    RegistryWriteResult,
)

logger = get_logger(__name__)

OptionsSerializer = Callable[[list[NormalizedSchemaOption]], dict[str, Any]]


class CRMRegistryWriter:
    """Persist provider-backed custom-field definitions for one integration scope."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
        entity_type: CRMCustomFieldEntity,
        provider_name: str,
        options_serializer: OptionsSerializer,
        log_label: str,
        automation_reconciler: AutomationTriggerSchemaReconciliationService | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.entity_type = entity_type
        self.provider_name = provider_name
        self.options_serializer = options_serializer
        self.log_label = log_label
        self.automation_reconciler = automation_reconciler or (
            AutomationTriggerSchemaReconciliationService(session, organisation_id)
        )

    async def apply(
        self,
        *,
        fields: list[NormalizedSchemaField],
    ) -> RegistryWriteResult:
        """Apply one sync pass with upsert + active-snapshot deactivation semantics."""

        existing = await self._load_existing()
        now = datetime.now(UTC)

        result = RegistryWriteResult()
        incoming_keys: set[str] = set()
        breaking_automation_keys: set[str] = set()

        for field in fields:
            incoming_keys.add(field.provider_field_key)
            row = existing.get(field.provider_field_key)
            if row is None:
                new_row = self._build_row(field, now=now)
                self.session.add(new_row)
                result.rows.append(new_row)
                result.created += 1
                continue

            was_active = bool(row.is_active)
            previous_key = row.key
            previous_type = _value(row.data_type)
            previous_automation_type = custom_field_type(row)
            self._apply_fields(row, field, now=now)
            row.is_active = True
            if (
                previous_key != row.key
                or previous_type != _value(row.data_type)
                or previous_automation_type != custom_field_type(row)
            ):
                breaking_automation_keys.add(previous_key)
            result.rows.append(row)
            if was_active:
                result.updated += 1
            else:
                result.reactivated += 1

        for key, row in existing.items():
            if key in incoming_keys:
                continue
            if row.is_active:
                row.is_active = False
                result.deactivated += 1
                breaking_automation_keys.add(row.key)

        await self.session.flush()
        if self.entity_type == CRMCustomFieldEntity.DEAL and breaking_automation_keys:
            await self.automation_reconciler.reconcile_custom_field_changes(
                breaking_automation_keys
            )
        logger.info(
            f"{self.log_label} registry write applied",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "rows_created": result.created,
                "rows_updated": result.updated,
                "rows_reactivated": result.reactivated,
                "rows_deactivated": result.deactivated,
            },
        )
        return result

    async def _load_existing(self) -> dict[str, CRMCustomFieldDefinition]:
        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.integration_id) == self.integration_id,
            col(CRMCustomFieldDefinition.entity_type) == self.entity_type.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.PROVIDER.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        rows = result.scalars().all()
        return {row.provider_field_key: row for row in rows if row.provider_field_key is not None}

    def _build_row(
        self,
        field: NormalizedSchemaField,
        *,
        now: datetime,
    ) -> CRMCustomFieldDefinition:
        return CRMCustomFieldDefinition(
            organisation_id=self.organisation_id,
            entity_type=self.entity_type,
            field_source=CRMCustomFieldSource.PROVIDER,
            integration_id=self.integration_id,
            provider_name=self.provider_name,
            key=field.key,
            provider_field_key=field.provider_field_key,
            label=field.label,
            description=field.description,
            data_type=field.data_type,
            required=False,
            is_predefined=False,
            is_provider_custom=field.is_provider_custom,
            display_order=field.display_order,
            options=self.options_serializer(field.options),
            validation=field.validation or {},
            formula=field.formula,
            data_payload=field.data_payload or None,
            source_modified_at=field.source_modified_at,
            last_seen_at=now,
            is_active=True,
            is_deleted=False,
        )

    def _apply_fields(
        self,
        row: CRMCustomFieldDefinition,
        field: NormalizedSchemaField,
        *,
        now: datetime,
    ) -> None:
        row.key = field.key
        row.label = field.label
        row.description = field.description
        row.data_type = field.data_type
        row.is_provider_custom = field.is_provider_custom
        row.display_order = field.display_order
        row.options = self.options_serializer(field.options)
        row.validation = field.validation or {}
        row.formula = field.formula
        row.data_payload = field.data_payload or None
        row.source_modified_at = field.source_modified_at
        row.last_seen_at = now


def _value(value: Any) -> Any:
    return getattr(value, "value", value)
