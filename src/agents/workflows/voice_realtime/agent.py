"""Agno-backed voice realtime agent built on the shared backend agent stack."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.agent import Agent as AgnoAgent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.skills import LocalSkills, Skills

from src.agents.context import AgentContext
from src.agents.timezone import get_agno_datetime_settings
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.voice_realtime.prompts import get_voice_realtime_system_prompt
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger
from src.service.orgnisation.kai_training_service import append_organisation_profile

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = get_logger(__name__)


class VoiceRealtimeAgent:
    """Voice-first conversational agent for the realtime experience."""

    def __init__(self, *, context: AgentContext):
        self.context = context
        self.org_name = context.org_name.strip() if context.org_name.strip() else "Our Organization"
        self.capabilities: AgentCapabilities | None = None
        self._tools: list[Callable[..., Any]] = get_tools_for_agent(
            "VoiceRealtimeAgent",
            context,
            lambda: self.capabilities,
        )

        config = agent_config.get_config("voice_realtime_agent")

        # Load skills from src/agents/skills using Agno progressive discovery
        # (same skills as GeneralAgent).
        skills_path = Path(__file__).parent.parent.parent / "skills"
        skills = Skills(loaders=[LocalSkills(str(skills_path))])

        self.agent = AgnoAgent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
                request_params={"parallel_tool_calls": True},
            ),
            description="Kai voice realtime assistant.",
            instructions=append_organisation_profile(
                get_voice_realtime_system_prompt(self.org_name),
                context.organisation_profile,
            ),
            markdown=False,
            debug_mode=config.debug_mode,
            telemetry=False,
            tools=self._tools,
            skills=skills,
            **get_agno_datetime_settings(context.metadata),
        )

    async def respond(self, user_message: str) -> str:
        """Return a plain-text response for a single user utterance."""
        prompt = user_message.strip() or "Hello."

        logger.info(
            "VoiceRealtimeAgent responding for org=%s with %d chars of input",
            self.org_name,
            len(prompt),
        )

        response = await self.agent.arun(prompt)
        content = response.content
        return content if isinstance(content, str) else str(content)
