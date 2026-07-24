"""Code-owned metadata for base agents shown in the agent template catalog.

The user API reads these definitions without a database session. Selecting a
template copies one exact metadata version into a normal user-owned agent, so
later code deployments cannot silently alter existing agents or their triggers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from src.api.schemas.agent_models import (
    AgentCreateRequest,
    AgentTemplateDetail,
    AgentTemplateSummary,
)
from src.domain.models.organisation_tables import AgentOutputMode

DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID = UUID("6e2f986d-3db4-4bf8-8e75-8fa596aa0101")

_DEAL_STAGE_HEALTH_REVIEW = AgentTemplateDetail(
    template_id=DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID,
    version=1,
    slug="deal-stage-health-review",
    name="Deal Stage Health Review",
    description=(
        "Reviews a deal after a stage change and returns an evidence-based "
        "health assessment with prioritized next actions."
    ),
    agent_type="general",
    model_name=None,
    agent_model_config={"temperature": 0.1},
    allowed_tools=[
        "get_deal_details",
        "get_deal_change_history",
        "list_deal_stakeholders",
        "get_existing_risks_blockers",
        "list_deal_emails",
        "list_deal_notes",
        "list_deal_tasks",
    ],
    allowed_skills=["deal-health-check"],
    max_turns=12,
    input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["schema_key", "event", "deal"],
        "properties": {
            "schema_key": {"const": "crm.deal.stage_changed.v1"},
            "event": {
                "type": "object",
                "required": [
                    "event_id",
                    "semantic_event_type",
                    "occurred_at",
                    "changed_fields",
                ],
                "properties": {
                    "event_id": {"type": "string", "format": "uuid"},
                    "semantic_event_type": {"const": "crm.deal.stage_changed"},
                    "occurred_at": {"type": "string", "format": "date-time"},
                    "changed_fields": {"type": "object"},
                },
                "additionalProperties": True,
            },
            "deal": {
                "type": "object",
                "required": ["deal_id", "outcome"],
                "properties": {
                    "deal_id": {"type": "string", "format": "uuid"},
                    "pipeline_id": {"type": ["string", "null"], "format": "uuid"},
                    "stage_id": {"type": ["string", "null"], "format": "uuid"},
                    "outcome": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    output_mode=AgentOutputMode.JSON,
    output_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "overall_health",
            "summary",
            "confidence",
            "risks",
            "recommended_actions",
        ],
        "properties": {
            "overall_health": {
                "type": "string",
                "enum": ["Green", "Yellow", "Red"],
            },
            "summary": {"type": "string"},
            "confidence": {
                "type": "string",
                "enum": ["High", "Medium", "Low"],
            },
            "risks": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "required": ["title", "evidence", "mitigation"],
                    "properties": {
                        "title": {"type": "string"},
                        "evidence": {"type": "string"},
                        "mitigation": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "recommended_actions": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "required": ["priority", "action", "reason"],
                    "properties": {
                        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                        "action": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
    prompt=(
        "You are a sales deal-health reviewer running automatically after a CRM deal "
        "stage change. Use the deal identity and stage-change event supplied in the run "
        "input. Inspect only evidence available through the allowed read tools. Decide "
        "whether the evidence supports the deal's current stage, identify the three most "
        "important risks, and recommend the next best actions for the deal owner. Never "
        "change CRM data, create tasks, send email, or contact anyone. Never invent "
        "missing facts; identify missing evidence explicitly and reduce confidence. "
        "Return only JSON matching the configured output schema. Do not expose internal "
        "UUIDs in narrative fields."
    ),
    published_at=datetime(2026, 7, 14, tzinfo=UTC),
)

_AGENT_TEMPLATES = (_DEAL_STAGE_HEALTH_REVIEW,)


def list_agent_templates() -> tuple[AgentTemplateDetail, ...]:
    """Return defensive copies of every base agent currently shipped by the backend."""

    return tuple(template.model_copy(deep=True) for template in _AGENT_TEMPLATES)


def get_agent_template(template_id: UUID, *, version: int) -> AgentTemplateDetail | None:
    """Resolve the exact code-owned version selected by the user interface."""

    for template in _AGENT_TEMPLATES:
        if template.template_id == template_id and template.version == version:
            return template.model_copy(deep=True)
    return None


def get_current_agent_template(template_id: UUID) -> AgentTemplateDetail | None:
    """Return the current metadata definition displayed by the template detail view."""

    for template in _AGENT_TEMPLATES:
        if template.template_id == template_id:
            return template.model_copy(deep=True)
    return None


def agent_template_summary(template: AgentTemplateDetail) -> AgentTemplateSummary:
    """Remove execution-only fields from a template catalog card."""

    return AgentTemplateSummary.model_validate(template.model_dump())


def agent_definition_from_template(template: AgentTemplateDetail) -> AgentCreateRequest:
    """Build the normal validated definition copied into a user-owned agent row."""

    return AgentCreateRequest(
        name=template.name,
        description=template.description,
        prompt=template.prompt,
        model_name=template.model_name,
        agent_model_config=template.agent_model_config.copy(),
        allowed_tools=list(template.allowed_tools),
        allowed_skills=list(template.allowed_skills),
        max_turns=template.max_turns,
        input_schema=template.input_schema.copy() if template.input_schema else None,
        output_mode=template.output_mode,
        output_schema=template.output_schema.copy() if template.output_schema else None,
    )
