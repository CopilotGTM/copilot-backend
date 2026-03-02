"""DealMemoryAgent — tool-calling agent that builds deal memory documents.

Reuses existing tools (list_deal_meetings, list_deal_stakeholders, etc.)
to gather data. Same pattern as DealSummaryAgent.
"""

from __future__ import annotations

from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.deal_memory.prompts import get_deal_memory_system_prompt
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class DealMemoryAgent:
    """Agent that builds or updates a deal memory document using tools."""

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID,
        publisher=None,
        org_name: str | None = None,
    ):
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.publisher = publisher
        self.org_name = org_name or "Our Organization"

        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=self.org_name,
        )

        tools = get_tools_for_agent("DealMemoryAgent", self.context, lambda: None)
        config = agent_config.get_config("deal_memory_agent")

        self.agent = Agent(
            model=AzureOpenAI(id=config.model, api_version=config.api_version),
            description="Deal Memory Agent",
            tools=tools,
            markdown=True,
            debug_mode=config.debug_mode,
        )

    async def generate(
        self,
        *,
        is_initial_build: bool,
        trigger_type: str | None = None,
        trigger_source_id: str | None = None,
        existing_memory: str = "",
    ) -> str:
        """Generate or update a deal memory document.

        Returns the complete markdown string.
        """
        system_prompt = get_deal_memory_system_prompt(
            self.org_name,
            is_initial=is_initial_build,
            trigger_type=trigger_type,
            trigger_source_id=trigger_source_id,
        )

        if is_initial_build:
            user_prompt = (
                "Build the initial deal memory for this deal. "
                "Gather all available data, then produce the complete "
                "markdown document following the output structure."
            )
        else:
            user_prompt = (
                f"Update this deal memory with the new {trigger_type or 'data'}.\n\n"
                f"## Existing Memory\n\n```markdown\n{existing_memory}\n```\n\n"
                "Gather the new information, then produce the complete "
                "updated markdown document."
            )

        logger.info(
            "DealMemoryAgent %s for deal %s",
            "initial build" if is_initial_build else "incremental update",
            self.deal_id,
        )

        response = await self.agent.arun(user_prompt, system_prompt=system_prompt)

        content = response.content
        if isinstance(content, str):
            return content
        return str(content)
