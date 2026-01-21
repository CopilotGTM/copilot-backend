"""Summary agent for generating meeting summaries from transcripts.

This agent uses tools to fetch the transcript and stakeholder data,
then produces a structured SummaryData output.

ARCHITECTURE: This agent does NOT hold database sessions during LLM calls.
Tools acquire sessions on-demand via context.acquire_session().
Sessions are released before any long-running operations (LLM calls).

SECURITY: organisation_id is baked into the context (immutable).
Tools cannot accept organisation_id as a parameter, preventing cross-org access.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.summary.models import SummaryData, SummaryInput
from src.agents.workflows.summary.prompts import (
    build_summary_user_prompt,
    get_summary_system_prompt,
)
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class SummaryAgent:
    """Agent for generating meeting summaries using tools.

    Uses Agno with tools to fetch transcript and stakeholder data,
    then produces a structured SummaryData response.

    ARCHITECTURE: This agent does NOT hold database sessions during LLM calls.
    Tools acquire sessions on-demand via context.acquire_session().
    This ensures connections are released before any long-running LLM operations.

    SECURITY: organisation_id is baked into the context (immutable).
    Tools use context.organisation_id and cannot accept it as a parameter.
    """

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
        org_name: str | None = None,
    ):
        """Initialize the agent with organisation and deal context.

        ARCHITECTURE: No database session is passed. Tools acquire sessions
        on-demand via context.acquire_session() and release them promptly.

        SECURITY BOUNDARIES (immutable, baked into context):
        - organisation_id: All tools use context.organisation_id for data access.
        - deal_id: All tools use context.deal_id - cannot query other deals.

        Args:
            organisation_id: Organisation UUID (IMMUTABLE - security boundary)
            deal_id: Deal UUID (IMMUTABLE - security boundary, tools cannot query other deals)
            publisher: Optional Redis publisher for progress updates
            org_name: Organisation display name for prompt personalization
        """
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.publisher = publisher

        # Build context for tools (no session - tools acquire on-demand)
        # SECURITY: Both organisation_id and deal_id are immutable in context
        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=org_name or "Our Organization",
        )

        # Get tools for this agent
        tools = self._get_tools()

        # Get model config
        config = agent_config.get_config("summary_agent")

        # Create agent with tools
        self.agent = Agent(
            model=AzureOpenAI(id=config.model, api_version=config.api_version),
            response_model=SummaryData,
            description=get_summary_system_prompt(self.context.org_name),
            tools=tools,
            show_tool_calls=config.show_tool_calls,
            markdown=True,
            debug_mode=config.debug_mode,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for meeting summary generation.

        Tools are defined in AGENT_TOOLS["SummaryAgent"] in registry.py.
        """
        # Get tools from central registry (no capabilities - not streaming to chat)
        return get_tools_for_agent("SummaryAgent", self.context, lambda: None)

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
