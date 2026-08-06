"""Regression tests for test catalog; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from uuid import uuid4

from src.api.schemas.automation_trigger_models import (
    TriggerCatalogPipelineOption,
    TriggerCatalogStageOption,
)
from src.service.automation.triggers.catalog import build_trigger_catalog


def _entries(catalog):
    return {
        trigger.trigger_key: trigger
        for category in catalog.categories
        for resource in category.resources
        for trigger in resource.triggers
    }


def test_catalog_serializes_registry_hierarchy_and_organisation_options() -> None:
    """Verify the contract that catalog serializes registry hierarchy and organisation options."""
    pipeline_id = uuid4()
    stage_id = uuid4()
    catalog = build_trigger_catalog(
        [
            TriggerCatalogPipelineOption(
                pipeline_id=pipeline_id,
                label="Sales Pipeline",
                is_active=True,
                stages=[
                    TriggerCatalogStageOption(
                        stage_id=stage_id,
                        label="Negotiation",
                        stage_state="open",
                        is_active=True,
                    )
                ],
            )
        ]
    )

    assert catalog.schema_version == 1
    assert catalog.limits.webhook_max_body_bytes == 60 * 1024
    assert catalog.limits.max_direct_subject_ids == 100
    assert catalog.limits.max_filter_group_levels == 2
    assert catalog.limits.max_filter_conditions == 20
    assert [category.category_key for category in catalog.categories][:4] == [
        "system",
        "time",
        "crm",
        "communication",
    ]
    crm = next(category for category in catalog.categories if category.category_key == "crm")
    assert [resource.resource_key for resource in crm.resources] == [
        "deal",
        "contact",
        "account",
    ]
    assert catalog.option_sets.pipelines[0].pipeline_id == pipeline_id
    assert catalog.option_sets.pipelines[0].stages[0].stage_id == stage_id
    assert [item.value for item in catalog.option_sets.enums["deal_statuses"]] == [
        "open",
        "won",
        "lost",
        "closed",
    ]


def test_catalog_exposes_generic_paths_operators_and_preset_metadata() -> None:
    """Verify the contract that catalog exposes generic paths operators and preset metadata."""
    entries = _entries(build_trigger_catalog([]))
    amount_crossed = entries["crm.deal.amount_crossed"]
    created_or_updated = entries["crm.deal.created_or_updated"]
    amount_path = next(field for field in amount_crossed.fields if field.path == "record.amount")

    assert amount_crossed.family == "event"
    assert amount_crossed.match_event_keys == ["crm.deal.updated"]
    assert amount_crossed.fixed_watch.model_dump() == {"mode": "any", "paths": ["amount"]}
    assert [field.key for field in amount_crossed.config_fields] == ["direction", "threshold"]
    assert amount_path.value_type == "decimal"
    assert amount_path.operators == [
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "is_empty",
        "not_empty",
    ]
    assert created_or_updated.match_event_keys == [
        "crm.deal.created",
        "crm.deal.updated",
    ]
    assert created_or_updated.allow_user_watch is True
    assert "amount" in created_or_updated.watch_paths


def test_catalog_hides_unavailable_producers_and_legacy_correlated_fields() -> None:
    """Verify the contract that catalog hides unavailable producers and legacy correlated fields."""
    catalog = build_trigger_catalog([])
    entries = _entries(catalog)

    assert "conversation.message.received" not in entries
    assert "agent.approval.requested" not in entries
    assert "meeting.scheduled" not in entries
    assert "meeting.transcript.ready" in entries
    assert "crm.lead.created" not in entries
    assert "crm.task.created" not in entries
    assert "crm.note.created" not in entries
    assert "ai.deal_risk.detected" in entries
    assert "ai.deal_blocker.resolved" in entries
    assert "ai.buyer_signal.detected" not in entries
    assert "agent.run.completed" in entries
    assert "agent.run.failed" in entries
    assert "agent.run.cancelled" not in entries

    serialized = entries["crm.deal.stage_changed"].model_dump()
    assert "trigger_type" not in serialized
    assert "source_domain" not in serialized
    assert "event_type" not in serialized
    assert serialized["fixed_watch"] == {"mode": "any", "paths": ["stage_id"]}


def test_catalog_keeps_ai_risk_and_blocker_resources_distinct() -> None:
    """Keep independently labelled AI sources from collapsing into one resource."""
    ai = next(
        category
        for category in build_trigger_catalog([]).categories
        if category.category_key == "ai"
    )

    resources = {resource.resource_key: resource for resource in ai.resources}

    assert resources["deal_risk"].label == "Deal risk"
    assert resources["deal_blocker"].label == "Deal blocker"
    assert "ai.deal_risk.detected" in {
        trigger.trigger_key for trigger in resources["deal_risk"].triggers
    }
    assert "ai.deal_blocker.resolved" in {
        trigger.trigger_key for trigger in resources["deal_blocker"].triggers
    }


def test_every_catalog_path_is_unique_and_declares_matcher_operators() -> None:
    """Verify the contract that every catalog path is unique and declares matcher operators."""
    for definition in _entries(build_trigger_catalog([])).values():
        paths = [field.path for field in definition.fields]
        assert len(paths) == len(set(paths))
        assert all(field.operators for field in definition.fields)
