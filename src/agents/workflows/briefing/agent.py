"""Briefing agent for generating meeting preparation briefings.

This agent follows a tool-first approach (Option D): it receives minimal identifiers
(deal_id, event_id, current_datetime) and uses tools to fetch meeting/deal context.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName, ToolRegistry
from src.agents.workflows.briefing.models import BriefingData, BriefingInput
from src.agents.workflows.briefing.prompts import (
    build_briefing_user_prompt,
    get_briefing_system_prompt,
)
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class BriefingAgent:
    """Agent for generating meeting briefings.

    Tool-first workflow: first fetch the target meeting details, classify call type,
    then pull only the additional context needed.
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

        # Get tools for this agent (for deeper analysis)
        tools = self._get_tools()

        # Create agent with tools
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5", api_version="2024-12-01-preview"),
            response_model=BriefingData,
            description=get_briefing_system_prompt(self.context.org_name),
            tools=tools,
            show_tool_calls=True,
            markdown=True,
            debug_mode=False,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for briefing generation.

        This agent is tool-first: it must fetch the target meeting details
        and then gather deal context via list tools.
        """
        # Create registry without capabilities (not streaming to chat)
        registry = ToolRegistry(self.context, lambda: None)

        return registry.get_tools(
            [
                # Research
                ToolName.SEARCH_INTERNAL_KNOWLEDGE,
                ToolName.SEARCH_INTERNET,
                # First-step: identify call type and meeting metadata
                ToolName.GET_MEETING_DETAILS,
                # Lightweight context discovery
                ToolName.LIST_DEAL_MEETINGS,
                ToolName.LIST_DEAL_EMAILS,
                ToolName.LIST_DEAL_STAKEHOLDERS,
                # Deep dives
                ToolName.GET_MEETING_TRANSCRIPT,
                ToolName.GET_EMAIL_DETAILS,
                ToolName.GET_STAKEHOLDER_DETAILS,
            ]
        )

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate(self, input_data: BriefingInput) -> BriefingData:
        """Generate a meeting briefing via a call-type-first, tool-driven workflow."""
        # Provide deal_id to tools as context default.
        self.context.deal_id = input_data.deal_id

        logger.info(
            "BriefingAgent generating briefing for meeting %s (deal %s)",
            input_data.event_id,
            input_data.deal_id,
        )

        self._publish_progress("Analyzing stakeholders and history...", "analysis", 0.4)

        # Build user prompt with meeting context
        user_prompt = build_briefing_user_prompt(input_data)

        self._publish_progress("AI is gathering information...", "tools", 0.5)

        # Run agent - it will use tools to gather data
        response = await self.agent.arun(user_prompt)

        self._publish_progress("Finalizing briefing...", "finalizing", 0.85)

        logger.info(
            "BriefingAgent completed briefing generation for meeting %s",
            input_data.event_id,
        )

        return cast(BriefingData, response.content)
