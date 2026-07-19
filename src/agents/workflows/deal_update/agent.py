"""DealUpdateAgent workflow that executes scoped CRM writes from tool results."""

from __future__ import annotations

from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.message import Message

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.context import AgentContext
from src.agents.timezone import get_agno_datetime_settings
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.deal_update.models import DealUpdateResult
from src.agents.workflows.deal_update.prompts import get_deal_update_system_prompt
from src.infra.config.agent_config import agent_config
from src.service.orgnisation.kai_training_service import append_organisation_profile


class DealUpdateAgent:
    """Deal-scoped agent that updates internal CRM deal state through tools."""

    def __init__(
        self,
        *,
        organisation_id: UUID,
        deal_id: UUID,
        org_name: str | None = None,
        organisation_profile: str | None = None,
    ):
        """Build a deal-update workflow bound to an org+deal boundary."""
        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=org_name or "Our Organization",
            organisation_profile=organisation_profile,
        )
        self.last_token_usage: TokenUsageData | None = None

        tools = get_tools_for_agent("DealUpdateAgent", self.context, lambda: None)
        config = agent_config.get_config("deal_update_agent")

        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
                request_params={"parallel_tool_calls": False},
            ),
            description="Deal Update Agent",
            tools=tools,
            output_schema=DealUpdateResult,
            markdown=False,
            **get_agno_datetime_settings(self.context.metadata),
            debug_mode=config.debug_mode,
            system_message=append_organisation_profile(
                get_deal_update_system_prompt(org_name), organisation_profile
            ),
            telemetry=False,
        )

    async def generate(
        self,
        *,
        trigger_type: str,
        trigger_source_id: str | None = None,
        instruction: str | None = None,
    ) -> DealUpdateResult:
        """Execute the update flow and return a typed result payload."""
        user_prompt = (
            f"Run deal update workflow for deal {self.context.deal_id}.\n"
            f"Trigger type: {trigger_type}\n"
            f"Trigger source id: {trigger_source_id or 'none'}\n"
            f"Instruction: {instruction or 'Use available deal context and tools.'}"
        )
        response = await self.agent.arun([Message(role="user", content=user_prompt)])
        self.last_token_usage = extract_token_usage(response, agent_name="DealUpdateAgent")
        if isinstance(response.content, DealUpdateResult):
            return response.content
        return DealUpdateResult.model_validate(response.content)
