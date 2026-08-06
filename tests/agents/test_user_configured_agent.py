"""Dedicated runtime contracts for user-configured agents."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.workflows.user_configured.agent import UserConfiguredAgent


class FakeResponse:
    """Minimal final response returned by the fake Agno agent."""

    content = "Call the buyer"


class FakeAgent:
    """Record constructor arguments and messages without making a model call."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.inputs: list[str] = []

    async def arun(self, *, input: str):  # noqa: A002 - mirrors Agno's public API
        """Record the final input and return a stable fake response."""

        self.inputs.append(input)
        return FakeResponse()


class FakeRegistry:
    """Return one executable placeholder for every requested registered tool."""

    def __init__(self) -> None:
        self.requested = []

    def get_tools(self, tool_names):
        """Record and resolve each tool name supplied by the runtime."""

        self.requested = tool_names
        return [lambda: None for _ in tool_names]


class EmptyRegistry:
    """Represent a worker runtime that cannot resolve a pinned tool."""

    def get_tools(self, _tool_names):
        """Return no capabilities to exercise fail-closed resolution."""

        return []


def agent_context() -> AgentContext:
    """Build immutable org, owner, and deal boundaries for the runtime tests."""

    run_id = uuid4()
    agent_id = uuid4()
    return AgentContext(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        user_id=uuid4(),
        metadata={
            "automation_agent_id": str(agent_id),
            "automation_agent_revision": 4,
            "automation_inference_run_id": str(run_id),
        },
    )


@pytest.mark.asyncio
async def test_agent_uses_only_pinned_configuration(tmp_path: Path) -> None:
    """A saved definition must fully control prompt, model, tools, and skills."""

    skill_dir = tmp_path / "pipeline-review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pipeline-review\ndescription: Review pipeline\n---\nUse CRM evidence.",
        encoding="utf-8",
    )
    registry = FakeRegistry()
    created: list[FakeAgent] = []
    tool_contexts: list[AgentContext] = []
    runtime = UserConfiguredAgent(
        context=agent_context(),
        prompt="Act as a deal coach.",
        model_name="gpt-5-mini",
        model_settings={"temperature": 0.2},
        allowed_tools=("search_deals",),
        allowed_skills=("pipeline-review",),
        max_turns=6,
        output_mode="text",
        output_schema=None,
        skills_dir=tmp_path,
        agent_factory=lambda **kwargs: created.append(FakeAgent(**kwargs)) or created[-1],
        model_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        tool_registry_factory=lambda context: tool_contexts.append(context) or registry,
    )

    response = await runtime.run('{"event": "deal.stage_changed"}')

    assert response.content == "Call the buyer"
    assert [tool.value for tool in registry.requested] == ["search_deals"]
    assert created[0].kwargs["description"] == "Act as a deal coach."
    assert created[0].kwargs["model"].id == "gpt-5-mini"
    assert created[0].kwargs["model"].request_params == {
        "temperature": 0.2,
        "parallel_tool_calls": True,
    }
    assert created[0].kwargs["skills"].get_skill_names() == ["pipeline-review"]
    assert created[0].inputs == ['{"event": "deal.stage_changed"}']
    assert tool_contexts[0].metadata["automation_model_name"] == "gpt-5-mini"
    assert tool_contexts[0].metadata["automation_prompt_version"].endswith(":r4")


def test_agent_rejects_a_missing_pinned_tool(tmp_path: Path) -> None:
    """Fail before execution when a worker cannot resolve a saved tool key."""

    with pytest.raises(ValueError, match="not registered"):
        UserConfiguredAgent(
            context=agent_context(),
            prompt="Act as a deal coach.",
            model_name="gpt-5-mini",
            model_settings={},
            allowed_tools=("search_deals",),
            allowed_skills=(),
            max_turns=6,
            output_mode="text",
            output_schema=None,
            skills_dir=tmp_path,
            agent_factory=FakeAgent,
            model_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            tool_registry_factory=lambda _context: EmptyRegistry(),
        )


def test_agent_rejects_a_missing_pinned_skill(tmp_path: Path) -> None:
    """Fail before execution when pinned skill content is absent from the worker."""

    with pytest.raises(ValueError, match="pinned skill is not available"):
        UserConfiguredAgent(
            context=agent_context(),
            prompt="Act as a deal coach.",
            model_name="gpt-5-mini",
            model_settings={},
            allowed_tools=(),
            allowed_skills=("missing-skill",),
            max_turns=6,
            output_mode="text",
            output_schema=None,
            skills_dir=tmp_path,
            agent_factory=FakeAgent,
            model_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            tool_registry_factory=lambda _context: FakeRegistry(),
        )
