"""Proactive reconciliation for CRM schema changes that break saved bindings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import (
    AutomationTrigger,
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
)
from src.service.automation.triggers.crm_schema import merge_deal_custom_fields
from src.service.automation.triggers.service import (
    AutomationTriggerSchemaReconciliationService,
)


def binding(
    organisation_id: UUID,
    *,
    watch_config: dict | None = None,
    filter_config: dict | None = None,
) -> AutomationTrigger:
    """Build the binding fixture used by the surrounding regression cases."""
    principal_id = uuid4()
    return AutomationTrigger(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        name="Deal automation",
        target_id=uuid4(),
        target_revision=1,
        target_snapshot={},
        trigger_key="crm.deal.updated",
        family="event",
        match_event_keys=["crm.deal.updated"],
        trigger_config={},
        watch_config=watch_config,
        filter_config=filter_config,
        execution_principal_type="user",
        execution_principal_id=principal_id,
        scope_type="owned_or_created_by_principal",
        audience_mode="principal",
        config_hash="a" * 64,
    )


class Session:
    """Represent the Session test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.flushed = 0

    async def flush(self) -> None:
        """Expose the flush operation expected by the Session test double."""
        self.flushed += 1


@pytest.mark.asyncio
async def test_deleted_custom_field_pauses_only_bindings_that_reference_its_path() -> None:
    """Verify the contract that deleted custom field pauses only bindings that reference its path."""
    organisation_id = uuid4()
    affected = binding(
        organisation_id,
        watch_config={"mode": "any", "paths": ["custom_properties.priority"]},
    )
    unaffected = binding(
        organisation_id,
        filter_config={"path": "record.name", "operator": "contains", "value": "Acme"},
    )
    session = Session()
    service = AutomationTriggerSchemaReconciliationService(
        session,  # type: ignore[arg-type]
        organisation_id,
    )
    service._list_active_deal_bindings = AsyncMock(  # type: ignore[method-assign]
        return_value=[affected, unaffected]
    )
    service.registry_service = SimpleNamespace(
        snapshot=AsyncMock(return_value=merge_deal_custom_fields([]))
    )

    paused = await service.reconcile_custom_field_changes({"priority"})

    assert paused == 1
    assert affected.status == "paused"
    assert affected.last_error_code == "custom_field_schema_changed"
    assert unaffected.status == "active"
    assert session.flushed == 1


@pytest.mark.asyncio
async def test_custom_field_type_change_pauses_even_when_new_matcher_type_still_validates() -> None:
    """Verify the contract that custom field type change pauses even when new matcher type still validates."""
    organisation_id = uuid4()
    affected = binding(
        organisation_id,
        filter_config={
            "path": "record.custom_properties.priority",
            "operator": "gt",
            "value": "10",
        },
    )
    field = CRMCustomFieldDefinition(
        organisation_id=organisation_id,
        key="priority",
        label="Priority",
        data_type=CRMCustomFieldDataType.NUMBER,
    )
    service = AutomationTriggerSchemaReconciliationService(
        Session(),  # type: ignore[arg-type]
        organisation_id,
    )
    service._list_active_deal_bindings = AsyncMock(  # type: ignore[method-assign]
        return_value=[affected]
    )
    service.registry_service = SimpleNamespace(
        snapshot=AsyncMock(return_value=merge_deal_custom_fields([field]))
    )

    assert await service.reconcile_custom_field_changes({"priority"}) == 1
    assert affected.status == "paused"
