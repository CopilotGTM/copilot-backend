"""Deal summary agent for generating comprehensive deal summaries.

This agent uses tools to gather information about stakeholders, meetings,
and emails, then synthesizes everything into a comprehensive deal summary.

ARCHITECTURE: This agent does NOT hold database sessions during LLM calls.
Tools acquire sessions on-demand via context.acquire_session().
Sessions are released before any long-running operations (LLM calls).

SECURITY BOUNDARIES (immutable, baked into context):
- organisation_id: Tools use context.organisation_id, cannot accept as parameter
- deal_id: Tools use context.deal_id, cannot query other deals
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI

from src.agents.common.token_tracking import TokenUsageData, extract_token_usage
from src.agents.context import AgentContext
from src.agents.timezone import get_agno_datetime_settings
from src.agents.tools.registry import get_tools_for_agent
from src.agents.workflows.deal_summary.models import DealHealthStatus, DealSummaryData
from src.agents.workflows.deal_summary.prompts import get_deal_summary_system_prompt
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger
from src.service.orgnisation.kai_training_service import (
    append_organisation_context,
    with_organisation_context_policy,
)

if TYPE_CHECKING:
    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class DealSummaryAgent:
    """Agent for generating comprehensive deal summaries using tools.

    Uses Agno with tools to gather data from stakeholders, meetings, and emails,
    then produces a structured DealSummaryData response.

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
        organisation_profile: str | None = None,
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
        self.last_token_usage: TokenUsageData | None = None

        # Build context for tools (no session - tools acquire on-demand)
        # SECURITY: Both organisation_id and deal_id are immutable in context
        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=org_name or "Our Organization",
            organisation_profile=organisation_profile,
        )

        # Get tools for this agent
        tools = self._get_tools()

        # Get model config
        config = agent_config.get_config("deal_summary_agent")

        # Create agent with tools
        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
                request_params={"parallel_tool_calls": True},
            ),
            output_schema=DealSummaryData,
            description=with_organisation_context_policy(
                get_deal_summary_system_prompt(self.context.org_name)
            ),
            tools=tools,
            markdown=True,
            **get_agno_datetime_settings(self.context.metadata),
            debug_mode=config.debug_mode,
            telemetry=False,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for deal summary generation.

        Tools are defined in AGENT_TOOLS["DealSummaryAgent"] in registry.py.
        """
        # Get tools from central registry (no capabilities - not streaming to chat)
        return get_tools_for_agent("DealSummaryAgent", self.context, lambda: None)

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate(self) -> DealSummaryData:
        """Generate a comprehensive deal summary.

        The agent will use tools to gather data about stakeholders, meetings,
        and communications, then synthesize into a summary.

        Returns:
            DealSummaryData with comprehensive deal summary
        """
        logger.info("DealSummaryAgent generating summary for deal %s", self.deal_id)

        self._publish_progress("Analyzing deal data with AI...", "analysis", 0.3)

        user_prompt = f"Input Deal Id {self.deal_id}. "

        self._publish_progress("AI is gathering information...", "tools", 0.5)

        response = await self.agent.arun(
            append_organisation_context(user_prompt, self.context.organisation_profile)
        )

        self.last_token_usage = extract_token_usage(response, agent_name="DealSummaryAgent")

        self._publish_progress("Finalizing summary...", "finalizing", 0.9)

        logger.info("DealSummaryAgent completed summary generation for deal %s", self.deal_id)

        content = response.content

        if isinstance(content, DealSummaryData):
            return content

        if isinstance(content, dict):
            try:
                return DealSummaryData.model_validate(content)
            except Exception:
                logger.warning(
                    "DealSummaryAgent returned dict that failed validation for deal %s",
                    self.deal_id,
                )

        # Azure outage or content filter — return empty so caller doesn't crash
        logger.warning(
            "DealSummaryAgent returned non-structured response for deal %s: %.200s",
            self.deal_id,
            str(content),
        )
        return DealSummaryData(
            deal_summary="",
            deal_health=DealHealthStatus.UNVALIDATED,
            deal_health_reason="",
        )
