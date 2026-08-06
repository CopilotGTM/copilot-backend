"""Automation reconciliation at the provider schema registry write boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.service.integrations.crm.schema.common.registry_writer import CRMRegistryWriter
from src.service.integrations.crm.schema.common.types import NormalizedSchemaField


class Session:
    """Represent the Session test double used by the surrounding regression cases."""

    def add(self, _row) -> None:  # noqa: ANN001
        """Expose the add operation expected by the Session test double."""
        return None

    async def flush(self) -> None:
        """Expose the flush operation expected by the Session test double."""
        return None


def row(*, label: str = "Priority") -> CRMCustomFieldDefinition:
    """Build the row fixture used by the surrounding regression cases."""
    return CRMCustomFieldDefinition(
        organisation_id=uuid4(),
        entity_type=CRMCustomFieldEntity.DEAL,
        field_source=CRMCustomFieldSource.PROVIDER,
        integration_id=uuid4(),
        provider_name="salesforce",
        provider_field_key="Priority__c",
        key="priority",
        label=label,
        data_type=CRMCustomFieldDataType.TEXT,
        is_active=True,
    )


def incoming(
    *,
    label: str,
    data_type: CRMCustomFieldDataType,
    formula: dict | None = None,
) -> NormalizedSchemaField:
    """Build the incoming fixture used by the surrounding regression cases."""
    return NormalizedSchemaField(
        provider_field_key="Priority__c",
        key="priority",
        label=label,
        description=None,
        data_type=data_type,
        is_provider_custom=True,
        formula=formula,
    )


def writer(existing: CRMCustomFieldDefinition, reconciler) -> CRMRegistryWriter:  # noqa: ANN001
    """Build the writer fixture used by the surrounding regression cases."""
    service = CRMRegistryWriter(
        session=Session(),  # type: ignore[arg-type]
        organisation_id=existing.organisation_id,
        integration_id=existing.integration_id,  # type: ignore[arg-type]
        entity_type=CRMCustomFieldEntity.DEAL,
        provider_name="salesforce",
        options_serializer=lambda _options: {},
        log_label="test",
        automation_reconciler=reconciler,
    )
    service._load_existing = AsyncMock(  # type: ignore[method-assign]
        return_value={"Priority__c": existing}
    )
    return service


@pytest.mark.asyncio
async def test_provider_type_change_reconciles_affected_automation_path() -> None:
    """Verify the contract that provider type change reconciles affected automation path."""
    existing = row()
    reconciler = SimpleNamespace(reconcile_custom_field_changes=AsyncMock(return_value=1))

    await writer(existing, reconciler).apply(
        fields=[incoming(label="Priority", data_type=CRMCustomFieldDataType.NUMBER)]
    )

    reconciler.reconcile_custom_field_changes.assert_awaited_once_with({"priority"})


@pytest.mark.asyncio
async def test_provider_label_change_does_not_reconcile_automation_path() -> None:
    """Verify the contract that provider label change does not reconcile automation path."""
    existing = row()
    reconciler = SimpleNamespace(reconcile_custom_field_changes=AsyncMock())

    await writer(existing, reconciler).apply(
        fields=[incoming(label="Deal priority", data_type=CRMCustomFieldDataType.TEXT)]
    )

    reconciler.reconcile_custom_field_changes.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_formula_result_type_change_reconciles_automation_path() -> None:
    """Verify the contract that provider formula result type change reconciles automation path."""
    existing = row()
    existing.data_type = CRMCustomFieldDataType.FORMULA
    existing.formula = {"result_type": "text"}
    reconciler = SimpleNamespace(reconcile_custom_field_changes=AsyncMock())

    await writer(existing, reconciler).apply(
        fields=[
            incoming(
                label="Priority",
                data_type=CRMCustomFieldDataType.FORMULA,
                formula={"result_type": "number"},
            )
        ]
    )

    reconciler.reconcile_custom_field_changes.assert_awaited_once_with({"priority"})
