"""Risk/Blocker extraction agent for identifying deal risks from content.

This agent uses tools to gather meeting transcripts, emails, and existing
risks/blockers, then identifies new potential risks and blockers that
could impact deal success. It includes deduplication logic to avoid
creating duplicate entries.

Follows the same pattern as DealSummaryAgent - receives minimal context
and uses tools to fetch the data it needs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName, ToolRegistry
from src.agents.workflows.risk_blocker_extraction.models import (
    RiskBlockerExtractionInput,
    RiskBlockerExtractionResult,
)
from src.agents.workflows.risk_blocker_extraction.prompts import (
    RISK_BLOCKER_EXTRACTION_SYSTEM_PROMPT,
    build_risk_blocker_extraction_user_prompt,
)
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class RiskBlockerExtractionAgent:
    """Agent for extracting risks and blockers from deal communications.

    Uses Agno with tools to gather data from meetings and emails,
    then produces a structured RiskBlockerExtractionResult response.
    Follows the same pattern as DealSummaryAgent.
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
            response_model=RiskBlockerExtractionResult,
            description=RISK_BLOCKER_EXTRACTION_SYSTEM_PROMPT,
            tools=tools,
            show_tool_calls=True,
            markdown=True,
            debug_mode=True,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for risk/blocker extraction.

        Returns:
            List of tool functions the agent can use
        """
        # Create registry without capabilities (not streaming to chat)
        registry = ToolRegistry(self.context, lambda: None)

        return registry.get_tools(
            [
                ToolName.GET_EXISTING_RISKS_BLOCKERS,
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

    async def extract(self, input_data: RiskBlockerExtractionInput) -> RiskBlockerExtractionResult:
        """Extract risks and blockers from deal communications.

        The agent will use tools to:
        1. Get existing risks/blockers for deduplication
        2. List and read meeting transcripts
        3. List and read email content
        4. Identify new risks/blockers from the communications

        Args:
            input_data: Minimal deal context with counts and date ranges

        Returns:
            RiskBlockerExtractionResult with extracted items
        """
        logger.info(
            "RiskBlockerExtractionAgent extracting for deal %s (%d meetings, %d emails, %d existing)",
            input_data.deal_id,
            input_data.meeting_count,
            input_data.email_count,
            input_data.existing_risks_count + input_data.existing_blockers_count,
        )

        self._publish_progress("Analyzing deal communications for risks...", "analysis", 0.2)

        # Build user prompt with deal context
        user_prompt = build_risk_blocker_extraction_user_prompt(
            deal_id=input_data.deal_id,
            deal_name=input_data.deal_name,
            company_name=input_data.company_name,
            deal_stage=input_data.deal_stage,
            meeting_count=input_data.meeting_count,
            email_count=input_data.email_count,
            existing_risks_count=input_data.existing_risks_count,
            existing_blockers_count=input_data.existing_blockers_count,
            first_meeting_date=input_data.first_meeting_date,
            last_meeting_date=input_data.last_meeting_date,
        )

        self._publish_progress("AI is analyzing meetings and emails...", "tools", 0.4)

        # Run agent - it will use tools to gather data
        response = await self.agent.arun(user_prompt)

        self._publish_progress("Processing identified risks and blockers...", "processing", 0.8)

        result = cast(RiskBlockerExtractionResult, response.content)

        # Update counts
        result.total_risks = sum(1 for i in result.items if i.item_type.value == "RISK")
        result.total_blockers = sum(1 for i in result.items if i.item_type.value == "BLOCKER")
        result.new_items_count = sum(1 for i in result.items if not i.is_duplicate)
        result.duplicate_items_count = sum(1 for i in result.items if i.is_duplicate)

        self._publish_progress("Finalizing extraction results...", "finalizing", 0.95)

        logger.info(
            "RiskBlockerExtractionAgent completed for deal %s: %d risks, %d blockers, %d new",
            input_data.deal_id,
            result.total_risks,
            result.total_blockers,
            result.new_items_count,
        )

        return result
