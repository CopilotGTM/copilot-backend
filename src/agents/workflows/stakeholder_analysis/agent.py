"""Stakeholder analysis agent for extracting insights from deal data.

This agent uses tools to gather meetings, emails, stakeholder data, and
deal memory, then produces structured DealStakeholdersAnalysisData output
analyzing all stakeholders' stance, decision power, engagement level,
motivations, and pain points.

ARCHITECTURE: This agent does NOT hold database sessions during LLM calls.
Tools acquire sessions on-demand via context.acquire_session().

SECURITY BOUNDARIES (immutable, baked into context):
- organisation_id: Tools use context.organisation_id, cannot accept as parameter
- deal_id: Tools use context.deal_id, cannot query other deals
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.stakeholder_analysis.models import DealStakeholdersAnalysisData
from src.agents.workflows.stakeholder_analysis.prompts import get_stakeholder_system_prompt
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class StakeholderAnalysisAgent:
    """Agent for analyzing all stakeholder insights using tools.

    Uses Agno with tools to gather meetings, emails, and stakeholder data,
    then produces a structured DealStakeholdersAnalysisData response.

    ARCHITECTURE: This agent does NOT hold database sessions during LLM calls.
    Tools acquire sessions on-demand via context.acquire_session().

    SECURITY: organisation_id and deal_id are baked into context (immutable).
    Tools cannot query data from other orgs or deals.
    """

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
        org_name: str | None = None,
    ):
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.publisher = publisher

        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=org_name or "Our Organization",
        )

        tools = self._get_tools()
        config = agent_config.get_config("stakeholder_analysis_agent")

        self.agent = Agent(
            model=AzureOpenAI(id=config.model, api_version=config.api_version),
            output_schema=DealStakeholdersAnalysisData,
            description=get_stakeholder_system_prompt(self.context.org_name),
            tools=tools,
            markdown=True,
            debug_mode=config.debug_mode,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for stakeholder analysis.

        Tools are defined in AGENT_TOOLS["StakeholderAnalysisAgent"] in registry.py.
        """
        return get_tools_for_agent("StakeholderAnalysisAgent", self.context, lambda: None)

    async def generate(self) -> DealStakeholdersAnalysisData:
        """Generate stakeholder analysis for all stakeholders in the deal.

        The agent uses tools to gather stakeholder data, meetings,
        emails, and deal memory, then synthesizes into analysis.

        Returns:
            DealStakeholdersAnalysisData with analysis for all stakeholders
        """
        logger.info("StakeholderAnalysisAgent generating analysis for deal %s", self.deal_id)

        user_prompt = (
            f"Analyze all stakeholders for deal {self.deal_id}. "
            "Gather all available data including stakeholder profiles, meetings, "
            "emails, and notes, then produce the structured analysis for each "
            "stakeholder and generate actionable insights."
        )

        response = await self.agent.arun(user_prompt)

        logger.info("StakeholderAnalysisAgent completed analysis for deal %s", self.deal_id)

        return cast(DealStakeholdersAnalysisData, response.content)
