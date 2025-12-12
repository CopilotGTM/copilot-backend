"""Deal summary agent for generating comprehensive deal summaries.

This agent uses tools to gather information about stakeholders, meetings,
and emails, then synthesizes everything into a comprehensive deal summary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName, ToolRegistry
from src.agents.workflows.deal_summary.models import DealSummaryData, DealSummaryInput
from src.agents.workflows.deal_summary.prompts import (
    DEAL_SUMMARY_SYSTEM_PROMPT,
    build_deal_summary_user_prompt,
)
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class DealSummaryAgent:
    """Agent for generating comprehensive deal summaries using tools.

    Uses Agno with tools to gather data from stakeholders, meetings, and emails,
    then produces a structured DealSummaryData response.
    """

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        """Initialize the agent with database context.

        Args:
            session: Async database session
            organisation_id: Organisation UUID
            publisher: Optional Redis publisher for progress updates
        """
        self.session = session
        self.organisation_id = organisation_id
        self.publisher = publisher

        # Build context for tools
        self.context = AgentContext(
            session=session,
            organisation_id=organisation_id,
        )

        # Get tools for this agent
        tools = self._get_tools()

        # Create agent with tools
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5", api_version="2024-12-01-preview"),
            response_model=DealSummaryData,
            description=DEAL_SUMMARY_SYSTEM_PROMPT,
            tools=tools,
            show_tool_calls=True,
            markdown=True,
            debug_mode=True,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for deal summary generation.

        Returns:
            List of tool functions the agent can use
        """
        # Create registry without capabilities (not streaming to chat)
        registry = ToolRegistry(self.context, lambda: None)

        return registry.get_tools(
            [
                ToolName.LIST_DEAL_STAKEHOLDERS,
                ToolName.GET_STAKEHOLDER_DETAILS,
                ToolName.LIST_DEAL_MEETINGS,
                ToolName.GET_MEETING_DETAILS,
                ToolName.GET_MEETING_TRANSCRIPT,
                ToolName.LIST_DEAL_EMAILS,
                ToolName.GET_EMAIL_DETAILS,
            ]
        )

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate(self, input_data: DealSummaryInput) -> DealSummaryData:
        """Generate a comprehensive deal summary.

        The agent will use tools to gather data about stakeholders, meetings,
        and communications, then synthesize into a summary.

        Args:
            input_data: Minimal deal context with counts and date ranges

        Returns:
            DealSummaryData with comprehensive deal summary
        """
        logger.info(
            "DealSummaryAgent generating summary for deal %s (%d meetings, %d stakeholders)",
            input_data.deal_id,
            input_data.meeting_count,
            input_data.stakeholder_count,
        )

        self._publish_progress("Analyzing deal data with AI...", "analysis", 0.3)

        # Build user prompt with deal context
        user_prompt = build_deal_summary_user_prompt(
            deal_name=input_data.deal_name,
            company_name=input_data.company_name,
            deal_stage=input_data.deal_stage,
            deal_amount=input_data.deal_amount,
            currency=input_data.currency,
            close_date=input_data.close_date,
            deal_status=input_data.deal_status,
            meeting_count=input_data.meeting_count,
            stakeholder_count=input_data.stakeholder_count,
            email_count=input_data.email_count,
            first_meeting_date=input_data.first_meeting_date,
            last_meeting_date=input_data.last_meeting_date,
            deal_id=input_data.deal_id,
        )

        self._publish_progress("AI is gathering information...", "tools", 0.5)

        # Run agent - it will use tools to gather data
        response = await self.agent.arun(user_prompt)

        self._publish_progress("Finalizing summary...", "finalizing", 0.9)

        logger.info("DealSummaryAgent completed summary generation for deal %s", input_data.deal_id)

        return cast(DealSummaryData, response.content)
