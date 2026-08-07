"""Regression tests for test registry; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.service.automation.events.contracts import (
    AutomationRouteMode,
    AutomationSourceOperation,
)
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    FIELD_OPERATORS,
    AutomationRegistry,
    EventDefinition,
    FieldType,
    TriggerDefinition,
    TriggerFamily,
)


def test_registry_rejects_duplicate_event_and_trigger_keys() -> None:
    """Verify the contract that registry rejects duplicate event and trigger keys."""
    event = EventDefinition(
        event_key="test.record.created",
        route_mode=AutomationRouteMode.MATCH,
        operation=AutomationSourceOperation.CREATE,
        subject_type="record",
    )
    trigger = TriggerDefinition(
        trigger_key="test.record.created",
        family=TriggerFamily.EVENT,
        category_key="test",
        category_label="Test",
        resource_key="record",
        resource_label="Record",
        event_label="Created",
        description="Created test record.",
        match_event_keys=(event.event_key,),
        subject_type="record",
    )

    with pytest.raises(ValueError, match="duplicate event_key"):
        AutomationRegistry((event, event), (trigger,))
    with pytest.raises(ValueError, match="duplicate trigger_key"):
        AutomationRegistry((event,), (trigger, trigger))


def test_registry_rejects_unknown_events_and_route_family_mismatch() -> None:
    """Verify the contract that registry rejects unknown events and route family mismatch."""
    event = EventDefinition(
        event_key="test.record.created",
        route_mode=AutomationRouteMode.MATCH,
        operation=AutomationSourceOperation.CREATE,
        subject_type="record",
    )
    trigger = TriggerDefinition(
        trigger_key="test.record.created",
        family=TriggerFamily.EVENT,
        category_key="test",
        category_label="Test",
        resource_key="record",
        resource_label="Record",
        event_label="Created",
        description="Created test record.",
        match_event_keys=("test.missing.created",),
        subject_type="record",
    )
    with pytest.raises(ValueError, match="unknown event"):
        AutomationRegistry((event,), (trigger,))

    direct_trigger = replace(
        trigger, match_event_keys=(event.event_key,), family=TriggerFamily.MANUAL
    )
    with pytest.raises(ValueError, match="direct family"):
        AutomationRegistry((event,), (direct_trigger,))


def test_catalog_hides_trigger_when_it_or_any_producer_is_unavailable() -> None:
    """Verify the contract that catalog hides trigger when it or any producer is unavailable."""
    event = EventDefinition(
        event_key="test.record.created",
        route_mode=AutomationRouteMode.MATCH,
        operation=AutomationSourceOperation.CREATE,
        subject_type="record",
        producer_available=False,
    )
    trigger = TriggerDefinition(
        trigger_key="test.record.created",
        family=TriggerFamily.EVENT,
        category_key="test",
        category_label="Test",
        resource_key="record",
        resource_label="Record",
        event_label="Created",
        description="Created test record.",
        match_event_keys=(event.event_key,),
        subject_type="record",
    )
    registry = AutomationRegistry((event,), (trigger,))

    assert registry.catalog_triggers() == ()


def test_operator_catalog_matches_domain_type_contract() -> None:
    """Verify the contract that operator catalog matches domain type contract."""
    assert FIELD_OPERATORS[FieldType.TEXT] == (
        "eq",
        "neq",
        "in",
        "not_in",
        "contains",
        "starts_with",
        "is_empty",
        "not_empty",
    )
    assert FIELD_OPERATORS[FieldType.DECIMAL] == (
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "is_empty",
        "not_empty",
    )
    assert FIELD_OPERATORS[FieldType.LIST] == (
        "contains",
        "contains_any",
        "contains_all",
        "is_empty",
        "not_empty",
    )


def test_deal_update_event_owns_current_and_before_after_path_contracts() -> None:
    """Verify the contract that deal update event owns current and before after path contracts."""
    definition = AUTOMATION_REGISTRY.event("crm.deal.updated")

    assert definition.fields["record.amount"].value_type == FieldType.DECIMAL
    assert definition.fields["record.amount"].accessor == ("record", "amount")
    assert definition.fields["changes.amount.before"].accessor == (
        "changes",
        "amount",
        "before",
    )
    assert definition.fields["changes.amount.after"].accessor == (
        "changes",
        "amount",
        "after",
    )
    assert definition.watch_fields["amount"].value_type == FieldType.DECIMAL


def test_created_or_updated_is_one_preset_subscribing_to_two_base_events() -> None:
    """Verify the contract that created or updated is one preset subscribing to two base events."""
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.created_or_updated")

    assert definition.match_event_keys == (
        "crm.deal.created",
        "crm.deal.updated",
    )
    assert definition.allow_user_watch is True


def test_source_selection_is_explicit_and_compiles_to_one_hidden_filter() -> None:
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.updated")
    deal_id = "11111111-1111-1111-1111-111111111111"

    assert definition.source_selector.option_source == "deals"
    assert definition.source_selector.filter_path == "record.id"
    assert definition.compile({"selection": {"mode": "all", "ids": []}}).filter is None

    selected = definition.compile({"selection": {"mode": "selected", "ids": [deal_id]}})

    assert selected.config == {"selection": {"mode": "selected", "ids": [deal_id]}}
    assert selected.filter == {
        "path": "record.id",
        "operator": "in",
        "value": [deal_id],
    }

    assert definition.compile({}).config == {"selection": {"mode": "all", "ids": []}}
    assert definition.compile({}) == definition.compile({"selection": {"mode": "all", "ids": []}})
    with pytest.raises(ValueError, match="unique"):
        definition.compile({"selection": {"mode": "selected", "ids": [deal_id, deal_id]}})


def test_source_selection_combines_after_a_semantic_fixed_rule() -> None:
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.entered_stage")
    deal_id = "11111111-1111-1111-1111-111111111111"

    compiled = definition.compile(
        {
            "stage_id": "stage-1",
            "selection": {"mode": "selected", "ids": [deal_id]},
        }
    )

    assert compiled.filter == {
        "all": [
            {
                "all": [
                    {
                        "path": "changes.stage_id.after",
                        "operator": "eq",
                        "value": "stage-1",
                    }
                ]
            },
            {"path": "record.id", "operator": "in", "value": [deal_id]},
        ]
    }


def test_amount_crossed_preset_compiles_up_and_down_without_float_math() -> None:
    """Verify the contract that amount crossed preset compiles up and down without float math."""
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.amount_crossed")

    upward = definition.compile({"direction": "up", "threshold": "100000.00"})
    downward = definition.compile({"direction": "down", "threshold": "100000.00"})

    assert definition.match_event_keys == ("crm.deal.updated",)
    assert upward.watch is not None
    assert upward.watch.paths == ("amount",)
    assert upward.filter == {
        "all": [
            {
                "path": "changes.amount.before",
                "operator": "lt",
                "value": "100000.00",
            },
            {
                "path": "changes.amount.after",
                "operator": "gte",
                "value": "100000.00",
            },
        ]
    }
    assert downward.filter == {
        "all": [
            {
                "path": "changes.amount.before",
                "operator": "gt",
                "value": "100000.00",
            },
            {
                "path": "changes.amount.after",
                "operator": "lte",
                "value": "100000.00",
            },
        ]
    }


@pytest.mark.parametrize(
    "config",
    [
        {"direction": "sideways", "threshold": "100.00"},
        {"direction": "up", "threshold": "NaN"},
        {"direction": "up", "threshold": 100.0},
        {"direction": "up"},
    ],
)
def test_amount_crossed_rejects_invalid_config(config) -> None:
    """Verify the contract that amount crossed rejects invalid config."""
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.amount_crossed")

    with pytest.raises(ValueError):
        definition.compile(config)


@pytest.mark.parametrize(
    ("trigger_key", "watch_path", "filter_path", "filter_value"),
    [
        ("crm.deal.entered_stage", "stage_id", "changes.stage_id.after", "stage-1"),
        ("crm.deal.exited_stage", "stage_id", "changes.stage_id.before", "stage-1"),
        ("crm.deal.won", "deal_status", "changes.deal_status.after", "won"),
        ("crm.deal.lost", "deal_status", "changes.deal_status.after", "lost"),
        ("crm.deal.reopened", "deal_status", "changes.deal_status.after", "open"),
    ],
)
def test_deal_specific_value_presets_compile_to_watch_and_filter(
    trigger_key,
    watch_path,
    filter_path,
    filter_value,
) -> None:
    """Verify the contract that deal specific value presets compile to watch and filter."""
    definition = AUTOMATION_REGISTRY.trigger(trigger_key)
    config = {"stage_id": filter_value} if trigger_key.endswith("_stage") else {}

    compiled = definition.compile(config)

    assert compiled.watch is not None
    assert compiled.watch.paths == (watch_path,)
    assert compiled.filter == {
        "all": [{"path": filter_path, "operator": "eq", "value": filter_value}]
    }


def test_destination_taxonomy_keeps_all_unimplemented_facts_hidden() -> None:
    """Verify the contract that destination taxonomy keeps all unimplemented facts hidden."""
    visible = {definition.trigger_key for definition in AUTOMATION_REGISTRY.catalog_triggers()}

    for trigger_key in (
        "conversation.message.received",
        "agent.approval.requested",
        "crm.lead.created",
        "crm.task.created",
        "crm.note.created",
        "ai.buyer_signal.detected",
        "agent.run.cancelled",
    ):
        assert trigger_key not in visible
    assert "ai.deal_risk.detected" in visible
    assert "ai.deal_blocker.resolved" in visible
    assert "agent.run.completed" in visible
    assert "agent.run.failed" in visible
    assert AUTOMATION_REGISTRY.event("conversation.message.received").producer_available is False
    assert AUTOMATION_REGISTRY.event("agent.approval.requested").producer_available is False


def test_ai_signal_triggers_are_organisation_only_without_owner_snapshot() -> None:
    """Verify the contract that ai signal triggers are organisation only without owner snapshot."""
    definition = AUTOMATION_REGISTRY.trigger("ai.deal_risk.detected")

    assert definition.allowed_principal_modes == ("organisation",)
    assert definition.allowed_scope_types == ("organisation",)
