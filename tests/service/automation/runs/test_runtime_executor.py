"""Snapshot-driven agent runtime contracts."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.agents.context import AgentContext
from src.service.automation.runs.runtime_executor import AgentRunExecutor
from src.service.automation.triggers.runtime import AgentRunRuntimeEnvelope


def envelope() -> AgentRunRuntimeEnvelope:
    """Build one internally consistent claimed-run snapshot for executor tests."""

    principal_id = uuid4()
    return AgentRunRuntimeEnvelope(
        run_id=uuid4(),
        attempt_number=1,
        worker_id="agent-worker-1",
        organisation_id=uuid4(),
        execution_principal_type="user",
        execution_principal_id=principal_id,
        principal_user_id=principal_id,
        audience_mode="principal",
        audience_config={},
        agent_id=uuid4(),
        agent_revision=2,
        trigger_id=uuid4(),
        event_id=uuid4(),
        event_key="crm.deal.updated",
        correlation_id="request-123",
        caused_by_run_id=None,
        causation_depth=0,
        subject_type="deal",
        subject_id=str(uuid4()),
        deal_id=uuid4(),
        agent_type="general",
        prompt="Act as a deal coach.",
        model_name="gpt-5-mini",
        model_config={"temperature": 0.2},
        allowed_tools=("search_deals",),
        allowed_skills=("pipeline-review",),
        max_turns=6,
        input_schema=None,
        output_mode="text",
        output_schema=None,
        task_goal="Prepare next steps",
        trigger_snapshot={"task_config": {"goal": "Prepare next steps"}},
        input_snapshot={"deal": {"name": "Acme"}},
    )


class FakeResponse:
    """Represent a successful final response without calling a model."""

    content = "Call the buyer"
    model = "gpt-5-mini"
    metrics = None


class FakeUserConfiguredAgent:
    """Record the immutable definition delegated by the durable-run executor."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.messages: list[str] = []
        self.model_name = kwargs.get("model_name") or "configured-default"

    async def run(self, message: str):
        """Record the event message and return a stable final response."""

        self.messages.append(message)
        return FakeResponse()


@pytest.mark.asyncio
async def test_executor_passes_pinned_snapshot_to_user_configured_agent() -> None:
    """Keep mutable definition rows out of execution after a run is claimed."""

    created_agents: list[FakeUserConfiguredAgent] = []

    def create_agent(**kwargs) -> FakeUserConfiguredAgent:
        """Capture the exact immutable definition delegated by the executor."""

        agent = FakeUserConfiguredAgent(**kwargs)
        created_agents.append(agent)
        return agent

    runtime = AgentRunExecutor(agent_factory=create_agent)
    item = envelope()
    context = AgentContext(
        organisation_id=item.organisation_id,
        deal_id=item.deal_id,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        user_id=item.principal_user_id,
    )

    result = await runtime.execute(item, context=context)

    assert created_agents[0].kwargs["context"] is context
    assert created_agents[0].kwargs["prompt"] == item.prompt
    assert created_agents[0].kwargs["model_name"] == item.model_name
    assert created_agents[0].kwargs["model_settings"] == item.model_config
    assert created_agents[0].kwargs["allowed_tools"] == item.allowed_tools
    assert created_agents[0].kwargs["allowed_skills"] == item.allowed_skills
    assert created_agents[0].kwargs["max_turns"] == item.max_turns
    assert created_agents[0].kwargs["output_mode"] == item.output_mode
    assert created_agents[0].kwargs["output_schema"] == item.output_schema
    assert '"name": "Acme"' in created_agents[0].messages[0]
    assert result.output_text == "Call the buyer"
    assert result.model_name == "gpt-5-mini"


@pytest.mark.asyncio
async def test_executor_rejects_context_outside_pinned_org_boundary() -> None:
    """Reject cross-organisation context before constructing any selected tool."""

    item = envelope()
    runtime = AgentRunExecutor()
    context = AgentContext(
        organisation_id=uuid4(),
        deal_id=item.deal_id,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        user_id=item.principal_user_id,
    )

    with pytest.raises(ValueError, match="organisation"):
        await runtime.execute(item, context=context)


@pytest.mark.asyncio
async def test_executor_validates_json_output_against_pinned_schema() -> None:
    """Preserve the output schema pinned at activation time."""

    item = replace(
        envelope(),
        allowed_tools=(),
        allowed_skills=(),
        output_mode="json",
        output_schema={
            "type": "object",
            "required": ["next_step"],
            "properties": {"next_step": {"type": "string"}},
        },
    )

    class JsonAgent(FakeUserConfiguredAgent):
        """Return structured content for the executor's schema validator."""

        async def run(self, message: str):
            """Record the event and return schema-conforming JSON content."""

            self.messages.append(message)
            return SimpleNamespace(content={"next_step": "Call buyer"}, metrics=None, model=None)

    runtime = AgentRunExecutor(agent_factory=JsonAgent)
    context = AgentContext(
        organisation_id=item.organisation_id,
        deal_id=item.deal_id,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        user_id=item.principal_user_id,
    )

    result = await runtime.execute(item, context=context)

    assert result.output_json == {"next_step": "Call buyer"}


@pytest.mark.asyncio
async def test_executor_runs_contact_subject_without_deal() -> None:
    """Contact-triggered agents preserve subject security without fake deal context."""

    contact_id = uuid4()
    item = replace(
        envelope(),
        subject_type="contact",
        subject_id=str(contact_id),
        deal_id=None,
        allowed_tools=(),
        allowed_skills=(),
        input_snapshot={"subject": {"type": "contact", "id": str(contact_id)}},
    )
    runtime = AgentRunExecutor(agent_factory=FakeUserConfiguredAgent)
    context = AgentContext(
        organisation_id=item.organisation_id,
        subject_type="contact",
        subject_id=str(contact_id),
        user_id=item.principal_user_id,
    )

    result = await runtime.execute(item, context=context)

    assert result.output_text == "Call the buyer"


def test_jsonschema_is_installed_in_every_runtime_image() -> None:
    """Keep structured agent validation available to both API and worker images."""

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert any(requirement.startswith("jsonschema") for requirement in project["dependencies"])


def test_worker_runtime_import_does_not_cross_into_api_only_packages() -> None:
    """The light-worker image must import its runtime without the uncopied API package."""

    script = """
import builtins

real_import = builtins.__import__

def reject_api_import(name, *args, **kwargs):
    if name == "src.api" or name.startswith("src.api."):
        raise AssertionError(f"worker runtime imported API-only module: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = reject_api_import
import src.service.automation.runs.runtime_executor
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline regression script
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
