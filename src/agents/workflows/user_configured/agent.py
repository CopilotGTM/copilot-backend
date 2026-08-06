"""Non-interactive runtime for one immutable user-agent definition.

The durable run executor supplies an `AgentContext` whose organisation, owner,
and deal identifiers are already pinned to the claimed run. This module owns
only model and capability assembly; queue delivery and run persistence remain
with the automation service.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agno.agent import Agent as AgnoAgent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.skills import LocalSkills, SkillLoader, Skills

from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName, ToolRegistry
from src.infra.config.agent_config import agent_config

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class UserConfiguredAgent:
    """Execute one saved definition inside immutable org, owner, and deal boundaries."""

    def __init__(
        self,
        *,
        context: AgentContext,
        prompt: str,
        model_name: str | None,
        model_settings: dict[str, Any],
        allowed_tools: tuple[str, ...],
        allowed_skills: tuple[str, ...],
        max_turns: int | None,
        output_mode: str,
        output_schema: dict[str, Any] | None,
        skills_dir: Path = DEFAULT_SKILLS_DIR,
        agent_factory: Callable[..., Any] = AgnoAgent,
        model_factory: Callable[..., Any] = AzureOpenAI,
        tool_registry_factory: Callable[[AgentContext], Any] | None = None,
    ) -> None:
        config = agent_config.get_config("user_configured_agent")
        effective_model = model_name or config.model
        model = model_factory(
            id=effective_model,
            api_version=config.api_version,
            retries=config.retries,
            delay_between_retries=1,
            exponential_backoff=True,
            request_params={**model_settings, "parallel_tool_calls": True},
        )

        agent_id = context.metadata.get("automation_agent_id")
        agent_revision = context.metadata.get("automation_agent_revision")
        prompt_version = (
            f"user-agent:{agent_id}:r{agent_revision}"
            if isinstance(agent_id, str)
            and agent_id
            and isinstance(agent_revision, int)
            and not isinstance(agent_revision, bool)
            else f"sha256:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}"
        )
        context = context.with_metadata(
            automation_model_name=effective_model,
            automation_prompt_version=prompt_version,
        )
        registry_factory = tool_registry_factory or (lambda item: ToolRegistry(item, lambda: None))
        registry = registry_factory(context)
        tool_names = [ToolName(key) for key in allowed_tools]
        tools = registry.get_tools(tool_names)
        # A saved capability must never silently disappear or broaden at runtime.
        # Failing before the model call keeps execution faithful to the snapshot.
        if len(tools) != len(tool_names):
            raise ValueError("one or more pinned tools are not registered in this runtime")

        skills = self._load_selected_skills(allowed_skills, skills_dir)
        structured = output_mode == "json"
        self.model_name = effective_model
        self.agent = agent_factory(
            model=model,
            description=prompt,
            structured_outputs=structured,
            output_schema=output_schema if structured else None,
            parse_response=True,
            markdown=not structured,
            tools=tools,
            tool_call_limit=max_turns or 150,
            skills=skills,
            telemetry=False,
            debug_mode=config.debug_mode,
        )

    async def run(self, message: str) -> Any:
        """Return the final response for one immutable trigger-event message."""

        return await self.agent.arun(input=message)

    @staticmethod
    def _load_selected_skills(
        skill_keys: tuple[str, ...],
        skills_dir: Path,
    ) -> Skills | None:
        """Load exactly the skill folders named by the claimed run snapshot."""

        if not skill_keys:
            return None
        loaders: list[SkillLoader] = []
        for key in skill_keys:
            path = skills_dir / key
            if not path.is_dir() or not (path / "SKILL.md").is_file():
                raise ValueError(f"pinned skill is not available: {key}")
            loaders.append(LocalSkills(str(path)))
        return Skills(loaders=loaders)
