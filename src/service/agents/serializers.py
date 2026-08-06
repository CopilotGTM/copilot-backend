"""Response serialization for persisted user-agent definitions.

The persistence model uses `model_settings` because Pydantic reserves `model_config`; this
module keeps that implementation detail behind the public `agent_model_config` field.
"""

from __future__ import annotations

from src.api.schemas.agent_models import (
    AgentAutomationEligibility,
    AgentDetail,
    AgentSummary,
    AutomationEligibilityMode,
)
from src.domain.models.organisation_tables import UserAgent
from src.service.automation.triggers.capabilities import RETRY_UNSAFE_AUTOMATION_TOOLS


def agent_summary(agent: UserAgent) -> AgentSummary:
    """Build the lightweight owner-list representation for one definition."""

    return AgentSummary(
        agent_id=agent.agent_id,
        revision=agent.revision,
        source_type=agent.source_type,
        status=agent.status,
        name=agent.name,
        description=agent.description,
        agent_type=agent.agent_type,
        model_name=agent.model_name,
        allowed_tools=agent.allowed_tools,
        allowed_skills=agent.allowed_skills,
        output_mode=agent.output_mode,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _automation_eligibility(agent: UserAgent, *, is_admin: bool) -> AgentAutomationEligibility:
    if set(agent.allowed_tools) & RETRY_UNSAFE_AUTOMATION_TOOLS:
        unavailable = AutomationEligibilityMode(
            available=False,
            reason_code="retry_unsafe_tools",
            reason="Automation is unavailable while this agent has tools that are not retry-safe.",
        )
        return AgentAutomationEligibility(personal=unavailable, organisation=unavailable)

    personal = AutomationEligibilityMode(available=True)
    if not is_admin:
        organisation = AutomationEligibilityMode(
            available=False,
            reason_code="organisation_requires_admin",
            reason="Organisation execution requires an organisation admin.",
        )
    elif agent.allowed_tools:
        organisation = AutomationEligibilityMode(
            available=False,
            reason_code="organisation_tools_not_supported",
            reason="Organisation execution is unavailable while this agent has tools.",
        )
    elif agent.allowed_skills:
        organisation = AutomationEligibilityMode(
            available=False,
            reason_code="organisation_skills_not_supported",
            reason="Organisation execution is unavailable while this agent has skills.",
        )
    else:
        organisation = AutomationEligibilityMode(available=True)
    return AgentAutomationEligibility(personal=personal, organisation=organisation)


def agent_detail(agent: UserAgent, *, is_admin: bool = False) -> AgentDetail:
    """Build the complete runnable definition returned to its owner."""

    summary = agent_summary(agent)
    return AgentDetail(
        **summary.model_dump(),
        owner_user_id=agent.owner_user_id,
        source_agent_id=agent.source_agent_id,
        source_version=agent.source_version,
        visibility=agent.visibility,
        prompt=agent.prompt,
        agent_model_config=agent.model_settings,
        max_turns=agent.max_turns,
        input_schema=agent.input_schema,
        output_schema=agent.output_schema,
        automation_eligibility=_automation_eligibility(agent, is_admin=is_admin),
    )


def agent_execution_snapshot(agent: UserAgent) -> dict[str, object]:
    """Freeze only fields needed to execute the exact pinned agent revision."""

    return {
        "agent_id": str(agent.agent_id),
        "revision": agent.revision,
        "source_type": getattr(agent.source_type, "value", agent.source_type),
        "source_agent_id": str(agent.source_agent_id) if agent.source_agent_id else None,
        "source_version": agent.source_version,
        "agent_type": agent.agent_type,
        "prompt": agent.prompt,
        "model_name": agent.model_name,
        "model_config": agent.model_settings,
        "allowed_tools": agent.allowed_tools,
        "allowed_skills": agent.allowed_skills,
        "max_turns": agent.max_turns,
        "input_schema": agent.input_schema,
        "output_mode": getattr(agent.output_mode, "value", agent.output_mode),
        "output_schema": agent.output_schema,
    }
