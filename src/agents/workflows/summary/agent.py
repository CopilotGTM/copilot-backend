"""Summary agent for generating meeting summaries from transcripts.

This agent uses tools to fetch the transcript and stakeholder data,
then produces a structured SummaryData output.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName, ToolRegistry
from src.agents.workflows.summary.models import SummaryData, SummaryInput
from src.agents.workflows.summary.prompts import (
    build_summary_user_prompt,
    get_summary_system_prompt,
)
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class SummaryAgent:
    """Agent for generating meeting summaries using tools.

    Uses Agno with tools to fetch transcript and stakeholder data,
    then produces a structured SummaryData response.
    """

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
        org_name: str | None = None,
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
            org_name=org_name or "Our Organization",
        )

        # Get tools for this agent
        tools = self._get_tools()

        # Create agent with tools
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
            response_model=SummaryData,
            description=get_summary_system_prompt(self.context.org_name),
            tools=tools,
            show_tool_calls=True,
            markdown=True,
            debug_mode=False,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for summary generation.

        Returns:
            List of tool functions the agent can use
        """
        # Create registry without capabilities (not streaming to chat)
        registry = ToolRegistry(self.context, lambda: None)

        return registry.get_tools(
            [
                ToolName.GET_MEETING_TRANSCRIPT,
                ToolName.GET_MEETING_DETAILS,
                ToolName.GET_MEETING_DEAL_CONTEXT,
                ToolName.LIST_DEAL_STAKEHOLDERS,
                ToolName.GET_STAKEHOLDER_DETAILS,
            ]
        )

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate(self, input_data: SummaryInput) -> SummaryData:
        """Generate a meeting summary using tools.

        The agent will use tools to fetch the transcript and stakeholder data,
        then analyze and generate a comprehensive summary.

        Args:
            input_data: Minimal meeting and deal context

        Returns:
            SummaryData with comprehensive meeting summary
        """
        logger.info(
            "SummaryAgent generating summary for meeting %s",
            input_data.event_id,
        )

        self._publish_progress("Fetching transcript...", "transcript", 0.4)

        # Build user prompt with meeting context
        user_prompt = build_summary_user_prompt(input_data)

        self._publish_progress("AI is analyzing meeting...", "analysis", 0.5)

        # Run agent - it will use tools to fetch transcript and stakeholders
        response = await self.agent.arun(user_prompt)

        self._publish_progress("Finalizing summary...", "finalizing", 0.85)

        logger.info(
            "SummaryAgent completed summary generation for meeting %s",
            input_data.event_id,
        )

        return cast(SummaryData, response.content)
