"""Risk/Blocker extraction agent for identifying deal risks from content.

This agent uses tools to gather meeting transcripts, emails, and existing
risks/blockers, then identifies new potential risks and blockers that
could impact deal success. It includes deduplication logic to avoid
creating duplicate entries.

ARCHITECTURE: This agent does NOT hold database sessions during LLM calls.
Tools acquire sessions on-demand via context.acquire_session().
Sessions are released before any long-running operations (LLM calls).

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
from src.agents.workflows.risk_blocker_extraction.models import RiskBlockerExtractionResult
from src.agents.workflows.risk_blocker_extraction.prompts import get_risk_blocker_system_prompt
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class RiskBlockerExtractionAgent:
    """Agent for extracting risks and blockers from deal communications.

    Uses Agno with tools to gather data from meetings and emails,
    then produces a structured RiskBlockerExtractionResult response.

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
        """Initialize the agent with organisation and deal context.

        ARCHITECTURE: No database session is passed. Tools acquire sessions
        on-demand via context.acquire_session() and release them promptly.

        SECURITY BOUNDARIES (immutable, baked into context):
        - organisation_id: All tools use context.organisation_id for data access
        - deal_id: All tools use context.deal_id - cannot query other deals

        Args:
            organisation_id: Organisation UUID (IMMUTABLE - security boundary)
            deal_id: Deal UUID (IMMUTABLE - security boundary)
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
        config = agent_config.get_config("risk_blocker_extraction_agent")

        # Create agent with tools
        self.agent = Agent(
            model=AzureOpenAI(id=config.model, api_version=config.api_version),
            output_schema=RiskBlockerExtractionResult,
            description="Risk and Blocker Extraction Agent for analyzing deal communications",
            tools=tools,
            tool_call_limit=100,
            debug_mode=config.debug_mode,
            system_message=get_risk_blocker_system_prompt(self.context.org_name),
            telemetry=False,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for risk/blocker extraction.

        Tools are defined in AGENT_TOOLS["RiskBlockerExtractionAgent"] in registry.py.
        """
        # Get tools from central registry (no capabilities - not streaming to chat)
        return get_tools_for_agent("RiskBlockerExtractionAgent", self.context, lambda: None)

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def extract(self) -> RiskBlockerExtractionResult:
        """Extract risks and blockers from deal communications.

        The agent will use tools to gather meetings, emails, existing
        risks/blockers, and other deal data, then identify new items.

        Returns:
            RiskBlockerExtractionResult with extracted items
        """
        logger.info("RiskBlockerExtractionAgent extracting for deal %s", self.deal_id)

        self._publish_progress("Analyzing deal communications for risks...", "analysis", 0.2)

        user_prompt = (
            f"Analyze deal {self.deal_id} for risks and blockers. Follow the steps exactly:\n"
            "1. Read the deal memory first — this is your primary context.\n"
            "2. Read ALL existing risks/blockers (include dismissed). For each: leave it as-is if still valid, update it if the assessment changed but it is still active, resolve it if the issue was real and later got addressed, and dismiss it if current evidence shows it should no longer be tracked as an active issue. Take action via tools.\n"
            "3. For any technical concerns, search internal knowledge to verify if they are actual gaps.\n"
            "4. Only drill into specific transcripts/emails if you need a direct quote or to verify an ambiguous signal.\n"
            "4a. When a tool returns source_record_id, copy that exact value into the new item.\n"
            "5. After managing existing items, identify genuinely NEW risks/blockers that pass all quality tests.\n"
            "Return only new items in your structured output. Existing item management is done via tools. It is acceptable to return zero new items."
        )

        logger.info("RiskBlockerExtractionAgent running agent for deal %s", self.deal_id)

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
            self.deal_id,
            result.total_risks,
            result.total_blockers,
            result.new_items_count,
        )

        return result
