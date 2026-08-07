"""CRM mutation-ledger to canonical automation event mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.service.automation.producers.crm import CRMEventAdapter


def mutation(event_type: str = "crm.deal.stage_changed") -> CRMDealMutationEvent:
    """Build the mutation fixture used by the surrounding regression cases."""
    organisation_id = uuid4()
    entity_id = uuid4()
    parent_run_id = uuid4()
    return CRMDealMutationEvent(
        organisation_id=organisation_id,
        event_type=event_type,
        entity_type="deal",
        entity_id=entity_id,
        deal_id=entity_id,
        operation="update_deal",
        occurred_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
        changed_fields={
            "stage_id": {"old": str(uuid4()), "new": str(uuid4())},
            "amount": {"old": "90000.00", "new": "125000.00"},
        },
        record_snapshot={
            "id": str(entity_id),
            "stage_id": str(uuid4()),
            "amount": "125000.00",
            "custom_properties": {"region": "apac"},
        },
        source="agent",
        actor_user_id=uuid4(),
        automation_source_mode="interactive",
        correlation_id="corr-123",
        caused_by_run_id=parent_run_id,
        causation_depth=2,
    )


@pytest.mark.parametrize(
    "event_type",
    [
        "crm.deal.updated",
        "crm.deal.stage_changed",
        "crm.deal.owner_changed",
        "crm.deal.account_changed",
        "crm.deal.primary_contact_changed",
        "crm.deal.closed",
        "crm.deal.reopened",
    ],
)
def test_existing_deal_operations_emit_one_base_update(event_type: str) -> None:
    """Verify the contract that existing deal operations emit one base update."""
    source = mutation(event_type)

    envelope = CRMEventAdapter(source.organisation_id).from_mutation(source)

    assert envelope is not None
    assert envelope.event_key == "crm.deal.updated"
    assert {key: value.model_dump() for key, value in envelope.changes.items()} == {
        key: {"before": value["old"], "after": value["new"]}
        for key, value in source.changed_fields.items()
    }
    assert envelope.record == source.record_snapshot
    assert envelope.actor.type == "agent"
    assert envelope.actor.id is None
    assert envelope.correlation_id == "corr-123"
    assert envelope.caused_by_run_id == source.caused_by_run_id
    assert envelope.causation_depth == 2


def test_create_and_delete_have_snapshots_without_synthetic_changes() -> None:
    """Verify the contract that create and delete have snapshots without synthetic changes."""
    created = mutation("crm.deal.created")
    deleted = mutation("crm.deal.deleted")

    create_event = CRMEventAdapter(created.organisation_id).from_mutation(created)
    delete_event = CRMEventAdapter(deleted.organisation_id).from_mutation(deleted)

    assert create_event is not None and create_event.event_key == "crm.deal.created"
    assert delete_event is not None and delete_event.event_key == "crm.deal.deleted"
    assert create_event.changes == delete_event.changes == {}


def test_agent_tool_source_is_attributed_to_agent_without_relabeling_user_id() -> None:
    """Verify the contract that agent tool source is attributed to agent without relabeling user id."""
    source = mutation()
    source.source = "agent_tool"

    envelope = CRMEventAdapter(source.organisation_id).from_mutation(source)

    assert envelope is not None
    assert envelope.actor.type == "agent"
    assert envelope.actor.id is None
    assert envelope.caused_by_run_id == source.caused_by_run_id


def test_update_emits_only_registry_fields_and_deal_custom_properties() -> None:
    """Keep audit-only deal fields out while preserving tenant custom-field changes."""
    source = mutation("crm.deal.updated")
    source.changed_fields["description"] = {"old": "Old", "new": "New"}
    source.changed_fields["custom_properties.region"] = {"old": "emea", "new": "apac"}

    envelope = CRMEventAdapter(source.organisation_id).from_mutation(source)

    assert envelope is not None
    assert set(envelope.changes) == {
        "amount",
        "custom_properties.region",
        "stage_id",
    }


def test_deal_account_change_uses_automation_account_id() -> None:
    """Map the deal model's company ID onto the public account ID event field."""
    source = mutation("crm.deal.account_changed")
    old_account_id = uuid4()
    new_account_id = uuid4()
    source.changed_fields = {"company_id": {"old": str(old_account_id), "new": str(new_account_id)}}
    source.record_snapshot["account_id"] = str(new_account_id)

    envelope = CRMEventAdapter(source.organisation_id).from_mutation(source)

    assert envelope is not None
    assert envelope.changes["account_id"].model_dump() == {
        "before": str(old_account_id),
        "after": str(new_account_id),
    }


def test_audit_only_contact_update_does_not_emit_automation_event() -> None:
    """A ledger-only contact edit must not create an unfilterable update event."""
    source = mutation("crm.contact.updated")
    source.entity_type = "contact"
    source.deal_id = None
    source.changed_fields = {"job_title": {"old": "CFO", "new": "COO"}}

    assert CRMEventAdapter(source.organisation_id).from_mutation(source) is None


def test_adapter_rejects_cross_tenant_and_ignores_unsupported_ledger_entities() -> None:
    """Verify the contract that adapter rejects cross tenant and ignores unsupported ledger entities."""
    source = mutation()
    with pytest.raises(ValueError, match="organisation"):
        CRMEventAdapter(uuid4()).from_mutation(source)

    source.entity_type = "custom_field"
    assert CRMEventAdapter(source.organisation_id).from_mutation(source) is None
