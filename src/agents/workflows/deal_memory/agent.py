"""DealMemoryAgent — tool-calling agent that builds deal memory documents.

Reuses existing tools (list_deal_meetings, list_deal_stakeholders, etc.)
to gather data. Same pattern as DealSummaryAgent.
"""

from __future__ import annotations

from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.message import Message

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.context import AgentContext
from src.agents.timezone import get_agno_datetime_settings
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.deal_memory.prompts import (
    get_deal_memory_system_prompt,
    get_expected_output_format,
)
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
        self.last_token_usage: TokenUsageData | None = None
        self.org_name = org_name or "Our Organization"

        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=self.org_name,
        )

        tools = get_tools_for_agent("DealMemoryAgent", self.context, lambda: None)
        config = agent_config.get_config("deal_memory_agent")

        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                reasoning_effort="high",
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
                request_params={"parallel_tool_calls": True},
            ),
            description="Deal Memory Agent",
            tools=tools,
            tool_call_limit=100,
            markdown=True,
            **get_agno_datetime_settings(self.context.metadata),
            debug_mode=config.debug_mode,
            system_message=get_deal_memory_system_prompt(self.org_name),
            telemetry=False,
        )

    async def generate(
        self,
        *,
        is_initial_build: bool,
        trigger_type: str | None = None,
        trigger_source_id: str | None = None,
    ) -> str:
        """Generate or update a deal memory document.

        Returns the complete markdown string.
        """
        system_prompt = get_deal_memory_system_prompt(self.org_name)
        logger.info("Generated system prompt for DealMemoryAgent:\n%s", system_prompt)
        user_prompt = f"Work On deal memory for deal {self.deal_id} based on new information..\n\n"

        # Include trigger info for incremental updates, to help the agent understand what new information it should focus on
        if not is_initial_build:
            user_prompt += (
                f"Trigger Type: {trigger_type}\nTrigger Source ID: {trigger_source_id}\n\n"
            )

        logger.info(
            "DealMemoryAgent %s for deal %s",
            "initial build" if is_initial_build else "incremental update",
            self.deal_id,
        )

        message_list = [
            Message(role="user", content=get_expected_output_format()),
            Message(role="user", content=user_prompt),
        ]

        response = await self.agent.arun(message_list)

        self.last_token_usage = extract_token_usage(response, agent_name="DealMemoryAgent")

        content = response.content
        if isinstance(content, str):
            return content
        return str(content)
