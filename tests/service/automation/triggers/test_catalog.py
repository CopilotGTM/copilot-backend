"""Backend-owned catalog tests for the first automation trigger definitions."""

from __future__ import annotations

from uuid import uuid4

from src.api.schemas.automation_trigger_models import (
    TriggerCatalogPipelineOption,
    TriggerCatalogStageOption,
)
from src.service.automation.triggers.catalog import build_trigger_catalog


def test_catalog_exposes_only_runnable_v1_trigger_contracts() -> None:
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

    entries = {
        entry.trigger_key: entry for category in catalog.categories for entry in category.triggers
    }
    categories = {
        category.category_key: [entry.trigger_key for entry in category.triggers]
        for category in catalog.categories
    }

    assert set(entries) == {
        "schedule",
        "crm.deal.created",
        "crm.deal.stage_changed",
        "crm.contact.created",
        "crm.contact.deleted",
        "crm.account.created",
        "crm.account.deleted",
    }
    assert categories["schedule"] == ["schedule"]
    assert categories["crm"] == [
        "crm.deal.created",
        "crm.deal.stage_changed",
        "crm.contact.created",
        "crm.contact.deleted",
        "crm.account.created",
        "crm.account.deleted",
    ]
    assert entries["schedule"].default_trigger_config == {
        "timezone": "UTC",
        "starts_at": None,
        "recurrence": "none",
    }
    assert entries["schedule"].source_domain is None
    assert entries["crm.deal.stage_changed"].default_filter_config == {
        "schema_key": "crm.deal.stage_changed.v1",
        "pipeline_ids": [],
        "stage_change_mode": "any_change",
        "from_stage_ids": [],
        "to_stage_ids": [],
        "outcome": "any",
    }
    assert [field.field_key for field in entries["crm.deal.stage_changed"].fields] == [
        "pipeline_ids",
        "stage_change_mode",
        "outcome",
        "from_stage_ids",
        "to_stage_ids",
    ]
    assert entries["crm.deal.created"].default_filter_config == {
        "schema_key": "crm.deal.created.v1",
        "pipeline_ids": [],
    }
    for event_type in (
        "crm.contact.created",
        "crm.contact.deleted",
        "crm.account.created",
        "crm.account.deleted",
    ):
        assert entries[event_type].default_filter_config == {"schema_key": f"{event_type}.v1"}
        assert entries[event_type].fields == []
    assert catalog.option_sets.pipelines[0].pipeline_id == pipeline_id
    assert catalog.option_sets.pipelines[0].stages[0].stage_id == stage_id
