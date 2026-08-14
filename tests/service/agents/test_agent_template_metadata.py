"""Regression contracts for code-owned system agent templates."""

from __future__ import annotations

import importlib
from uuid import UUID

from src.domain.models.organisation_tables import AgentOutputMode
from src.service.agents.validator import AgentDefinitionValidator


def test_system_template_metadata_exposes_stable_deal_health_agent() -> None:
    """Keep the first system template addressable across deployments and user copies."""

    metadata = importlib.import_module("src.service.agents.template_metadata")

    templates = metadata.list_agent_templates()

    assert len(templates) == 1
    template = templates[0]
    assert template.template_id == UUID("6e2f986d-3db4-4bf8-8e75-8fa596aa0101")
    assert template.slug == "deal-stage-health-review"
    assert template.version == 2
    assert template.name == "Deal Stage Health Review"


def test_deal_health_template_is_runnable_with_read_only_capabilities() -> None:
    """Prevent a base sales template from gaining CRM mutations or outbound actions."""

    metadata = importlib.import_module("src.service.agents.template_metadata")

    template = metadata.list_agent_templates()[0]
    definition = metadata.agent_definition_from_template(template)
    AgentDefinitionValidator().validate_definition(definition)

    assert definition.agent_model_config == {}
    assert definition.output_mode == AgentOutputMode.TEXT
    assert definition.output_schema is None
    assert definition.allowed_skills == ["deal-health-check"]
    assert definition.allowed_tools == [
        "get_deal_details",
        "get_deal_change_history",
        "list_deal_stakeholders",
        "get_existing_risks_blockers",
        "list_deal_emails",
        "list_deal_notes",
        "list_deal_tasks",
    ]
    assert set(definition.allowed_tools).isdisjoint(
        {
            "add_deal_note",
            "create_task",
            "send_email",
            "update_deal_fields",
            "update_deal_stage",
        }
    )


def test_template_lookup_requires_the_exact_current_version() -> None:
    """Never replace the version selected by the UI with a different definition."""

    metadata = importlib.import_module("src.service.agents.template_metadata")
    template_id = UUID("6e2f986d-3db4-4bf8-8e75-8fa596aa0101")

    assert metadata.get_agent_template(template_id, version=2) is not None
    assert metadata.get_agent_template(template_id, version=1) is None
    assert metadata.get_agent_template(UUID(int=0), version=1) is None
