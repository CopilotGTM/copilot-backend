"""StakeholderResearchAgent — researches a single stakeholder via Apollo/LinkedIn.

Returns a StakeholderResearchResult (structured output) instead of raw markdown.
The service layer renders markdown and persists all DB fields.
"""

from __future__ import annotations

import json
from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.message import Message
from pydantic import ValidationError

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.context import AgentContext
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.stakeholder_research.models import StakeholderResearchResult
from src.agents.workflows.stakeholder_research.prompts import (
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
            output_schema=StakeholderResearchResult,
            debug_mode=config.debug_mode,
            system_message=get_stakeholder_research_system_prompt(self.org_name),
            telemetry=False,
        )

    async def research(
        self,
        stakeholder_id: UUID,
        email: str | None = None,
        name: str | None = None,
        linkedin_url: str | None = None,
    ) -> StakeholderResearchResult:
        """Research a single stakeholder. Returns structured result."""
        identity_parts = []
        if name:
            identity_parts.append(f"name={name}")
        if email:
            identity_parts.append(f"email={email}")
        if linkedin_url:
            identity_parts.append(f"linkedin_url={linkedin_url}")
        identity = ", ".join(identity_parts) or f"stakeholder_id={stakeholder_id}"

        user_prompt = (
            f"Research this stakeholder and populate the structured output.\n\n"
            f"Stakeholder ID: {stakeholder_id}\n"
            f"Identity: {identity}\n\n"
            f'Start by calling research_stakeholder with stakeholder_id="{stakeholder_id}".'
        )

        logger.info(
            "StakeholderResearchAgent researching stakeholder %s (%s)",
            stakeholder_id,
            identity,
        )

        message_list = [
            Message(role="user", content=user_prompt),
        ]

        response = await self.agent.arun(message_list)

        self.last_token_usage = extract_token_usage(response, agent_name="StakeholderResearchAgent")

        return self._normalize_output(response.content)

    @staticmethod
    def _normalize_output(content: object) -> StakeholderResearchResult:
        """Normalize agent output into StakeholderResearchResult.

        Handles cases where the LLM returns the Pydantic model directly,
        a dict, a JSON string, or something unexpected (content filter, etc.).
        """
        if isinstance(content, StakeholderResearchResult):
            return content

        if isinstance(content, dict):
            try:
                return StakeholderResearchResult.model_validate(content)
            except ValidationError:
                logger.warning("Dict output failed validation: %.200s", str(content))

        if isinstance(content, str):
            stripped = content.strip()

            try:
                return StakeholderResearchResult.model_validate_json(stripped)
            except (ValidationError, json.JSONDecodeError):
                pass

            try:
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    return StakeholderResearchResult.model_validate(payload)
            except (ValidationError, json.JSONDecodeError, TypeError):
                pass

        logger.warning(
            "StakeholderResearchAgent returned non-structured response: %.200s",
            str(content),
        )
        return StakeholderResearchResult(
            status="error",
            error_detail="Agent returned unstructured response",
        )
