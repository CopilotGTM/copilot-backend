"""StakeholderResearchAgent — researches a single stakeholder via Apollo/LinkedIn.

Follows the DealMemoryAgent pattern:
- AgentContext with immutable organisation_id security boundary
- Tools from registry (ENRICH_STAKEHOLDER, ENRICH_LINKEDIN_PROFILE, RESEARCH_LINKEDIN_PROFILE)
- Token usage tracking via extract_token_usage
- One agent call per stakeholder (no batching)
"""

from __future__ import annotations

from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.message import Message

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.context import AgentContext
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.stakeholder_research.prompts import (
    get_expected_output_format,
    get_stakeholder_research_system_prompt,
)
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class StakeholderResearchAgent:
    """Agent that researches a single stakeholder using Apollo/LinkedIn tools."""

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID | None = None,
        org_name: str | None = None,
    ):
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.last_token_usage: TokenUsageData | None = None
        self.org_name = org_name or "Our Organization"

        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=self.org_name,
        )

        tools = get_tools_for_agent("StakeholderResearchAgent", self.context, lambda: None)
        config = agent_config.get_config("stakeholder_research_agent")

        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
            ),
            description="Stakeholder Research Agent",
            tools=tools,
            tool_call_limit=10,
            markdown=True,
            debug_mode=config.debug_mode,
            system_message=get_stakeholder_research_system_prompt(self.org_name),
            telemetry=False,
        )

    async def research(
        self,
        stakeholder_id: UUID,
        email: str,
        name: str | None = None,
    ) -> str:
        """Research a single stakeholder. Returns markdown profile."""
        identity = f"email={email}"
        if name:
            identity = f"name={name}, {identity}"

        user_prompt = (
            f"Research this stakeholder and produce a markdown profile.\n\n"
            f"Stakeholder ID: {stakeholder_id}\n"
            f"Identity: {identity}\n\n"
            f'Start by calling enrich_stakeholder with stakeholder_id="{stakeholder_id}".'
        )

        logger.info(
            "StakeholderResearchAgent researching stakeholder %s (%s)",
            stakeholder_id,
            identity,
        )

        message_list = [
            Message(role="user", content=get_expected_output_format()),
            Message(role="user", content=user_prompt),
        ]

        response = await self.agent.arun(message_list)

        self.last_token_usage = extract_token_usage(response, agent_name="StakeholderResearchAgent")

        content = response.content
        if isinstance(content, str):
            return content
        return str(content)
