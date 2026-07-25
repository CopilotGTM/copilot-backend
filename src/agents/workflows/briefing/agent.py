"""Briefing agent for generating meeting preparation briefings.

This agent follows a tool-first approach (Option D): it receives minimal identifiers
(deal_id, event_id, current_datetime) and uses tools to fetch meeting/deal context.

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
from src.agents.workflows.briefing.models import BriefingData, BriefingInput
from src.agents.workflows.briefing.prompts import (
    build_briefing_user_prompt,
    get_briefing_system_prompt,
)
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger
from src.service.orgnisation.kai_training_service import (
    append_organisation_context,
    with_organisation_context_policy,
)

if TYPE_CHECKING:
    from src.service.crm.jobs.deal_job_event_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class BriefingAgent:
    """Agent for generating meeting briefings.

    The tool-first workflow fetches the target event before gathering account,
    participant, and interaction context. Its prompt decides which optional
    research is worth doing instead of classifying the event into a call type.

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

        tools = self._get_tools()
        config = agent_config.get_config("briefing_agent")

        # The model may collect independent baseline sources concurrently, while
        # the schema keeps the externally stored result to one stable field.
        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
                request_params={"parallel_tool_calls": True},
            ),
            output_schema=BriefingData,
            description=with_organisation_context_policy(
                get_briefing_system_prompt(self.context.org_name)
            ),
            tools=tools,
            markdown=True,
            **get_agno_datetime_settings(self.context.metadata),
            debug_mode=config.debug_mode,
            telemetry=False,
        )

    def _get_tools(self) -> list[Callable]:
        """Get tools for briefing generation.

        Tools are defined in AGENT_TOOLS["BriefingAgent"] in registry.py.
        This agent is tool-first: fetches meeting details then gathers deal context.
        """
        return get_tools_for_agent("BriefingAgent", self.context, lambda: None)

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate(self, input_data: BriefingInput) -> BriefingData:
        """Generate an adaptive, evidence-led briefing with read-only tools.

        SECURITY: deal_id is already baked into context (immutable).
        Tools use context.deal_id and cannot query other deals.
        """
        logger.info(
            "BriefingAgent generating briefing for meeting %s (deal %s)",
            input_data.event_id,
            self.deal_id,
        )

        self._publish_progress("Analyzing stakeholders and history...", "analysis", 0.4)

        # Build user prompt with meeting context
        user_prompt = build_briefing_user_prompt(input_data)

        self._publish_progress("AI is gathering information...", "tools", 0.5)

        # Run agent - it will use tools to gather data
        response = await self.agent.arun(
            append_organisation_context(user_prompt, self.context.organisation_profile)
        )

        self.last_token_usage = extract_token_usage(response, agent_name="BriefingAgent")

        self._publish_progress("Finalizing briefing...", "finalizing", 0.85)

        logger.info(
            "BriefingAgent completed briefing generation for meeting %s",
            input_data.event_id,
        )

        content = response.content
        if isinstance(content, BriefingData):
            return content

        logger.warning(
            "BriefingAgent returned non-structured response for meeting %s: %.200s",
            input_data.event_id,
            str(content),
        )
        return BriefingData(briefing_overview="")
