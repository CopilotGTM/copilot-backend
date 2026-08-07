"""Display-state projections for saved automation triggers."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.automation.triggers.matcher import combine_filter_trees
from src.service.automation.triggers.options import TriggerOptionRecord
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY
from src.service.automation.triggers.serializers import trigger_detail, trigger_summary


def _trigger(**overrides):
    values = {
        "trigger_id": uuid4(),
        "name": "Scheduled review",
        "target_type": "agent",
        "target_id": uuid4(),
        "target_revision": 1,
        "trigger_key": "time.schedule.due",
        "family": "schedule",
        "match_event_keys": ["time.schedule.due"],
        "execution_principal_type": "user",
        "status": "active",
        "next_trigger_at": None,
        "last_triggered_at": None,
        "trigger_count": 1,
        "consecutive_failures": 0,
        "last_error_code": None,
        "last_error": None,
        "trigger_config": {"recurrence": "none"},
        "target_snapshot": {"revision": 1},
        "config_schema_version": 1,
        "watch_config": None,
        "filter_config": None,
        "execution_principal_id": uuid4(),
        "scope_type": "owned_or_created_by_principal",
        "scope_config": {},
        "audience_mode": "principal",
        "audience_config": {},
        "task_config": {"goal": None},
        "delivery_config": {"mode": "none"},
        "config_hash": "a" * 64,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("overrides", "display_status"),
    [
        ({}, "completed"),
        ({"trigger_config": {"recurrence": "daily"}}, "active"),
        ({"last_error": "Scheduler failed"}, "needs_attention"),
    ],
)
def test_trigger_summary_derives_truthful_display_status(overrides, display_status) -> None:
    summary = trigger_summary(_trigger(**overrides), current_target_revision=1)

    assert summary.display_status == display_status
    assert summary.presentation.breadcrumb == "Time / Schedule / Scheduled time"
    assert summary.presentation.authority_label == "Runs as you"
    assert summary.presentation.scope_summary == "Scheduled runs under your saved user identity"


def test_trigger_summary_presents_resolved_labels_without_stable_ids() -> None:
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.updated")
    deal_id = str(uuid4())
    owner_id = str(uuid4())
    config = {"selection": {"mode": "selected", "ids": [deal_id]}}
    compiled = definition.compile(config)
    user_filter = {"path": "record.owner_id", "operator": "eq", "value": owner_id}
    trigger = _trigger(
        trigger_key=definition.trigger_key,
        family="event",
        match_event_keys=list(definition.match_event_keys),
        trigger_config=config,
        filter_config=combine_filter_trees(compiled.filter, user_filter),
    )
    resolved = {
        ("deals", deal_id): TriggerOptionRecord(deal_id, "Acme renewal", "active", True),
        ("crm_users", owner_id): TriggerOptionRecord(owner_id, "Former owner", "inactive", False),
    }

    summary = trigger_summary(
        trigger,
        current_target_revision=1,
        definition=definition,
        resolved_options=resolved,
    )

    assert summary.presentation.source_summary == "Specific deals: Acme renewal"
    assert summary.presentation.conditions[0].field_label == "Owner"
    assert summary.presentation.conditions[0].value_labels == ["Former owner"]
    assert summary.presentation.conditions[0].status == "needs_attention"
    assert "Needs attention" in summary.presentation.warnings
    assert deal_id not in summary.model_dump_json()
    assert owner_id not in summary.model_dump_json()


def test_trigger_detail_returns_only_user_decisions_for_a_semantic_preset() -> None:
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.entered_stage")
    fixed = definition.compile({"stage_id": "stage-1"})
    user_filter = {"path": "record.amount", "operator": "gte", "value": "100000"}
    trigger = _trigger(
        trigger_key=definition.trigger_key,
        family="event",
        match_event_keys=list(definition.match_event_keys),
        trigger_config=fixed.config,
        watch_config={"mode": fixed.watch.mode, "paths": list(fixed.watch.paths)},
        filter_config={"all": [fixed.filter, user_filter]},
    )

    detail = trigger_detail(trigger, current_target_revision=1, definition=definition)

    assert detail.watch is None
    assert detail.filter == user_filter


def test_trigger_detail_marks_an_unseparable_matcher_as_needing_attention() -> None:
    definition = AUTOMATION_REGISTRY.trigger("crm.deal.won")
    fixed = definition.compile({})
    user_filter = {"path": "record.amount", "operator": "gte", "value": "100000"}
    trigger = _trigger(
        trigger_key=definition.trigger_key,
        family="event",
        match_event_keys=list(definition.match_event_keys),
        trigger_config=fixed.config,
        watch_config={"mode": fixed.watch.mode, "paths": list(fixed.watch.paths)},
        filter_config={"any": [fixed.filter, user_filter]},
    )

    detail = trigger_detail(trigger, current_target_revision=1, definition=definition)

    assert detail.display_status == "needs_attention"
    assert detail.watch is None
    assert detail.filter is None
