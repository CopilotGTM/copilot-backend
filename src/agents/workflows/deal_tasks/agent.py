"""Deal task automation agent for post-meeting task synchronization.

ARCHITECTURE: This agent does not hold database sessions during LLM calls.
Tools acquire sessions on-demand via AgentContext.

SECURITY BOUNDARIES:
- organisation_id: immutable tenant boundary for all tools
- deal_id: immutable deal boundary for task reads/writes
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
from src.agents.workflows.deal_tasks.models import DealTaskSyncResult
from src.agents.workflows.deal_tasks.prompts import get_deal_task_system_prompt
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher

logger = get_logger(__name__)


class DealTaskAgent:
    """Agent that creates and completes internal deal tasks from meeting summaries."""

    def __init__(
        self,
        organisation_id: UUID,
        deal_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
        org_name: str | None = None,
    ):
        """Build a deal-scoped automation agent with immutable tenant context."""
        self.organisation_id = organisation_id
        self.deal_id = deal_id
        self.publisher = publisher
        self.last_token_usage: TokenUsageData | None = None
        # Keep org/deal fixed in AgentContext so every tool call stays tenant-bound.
        self.org_name = org_name or "Our Organization"

        self.context = AgentContext(
            organisation_id=organisation_id,
            deal_id=deal_id,
            org_name=self.org_name,
        )

        config = agent_config.get_config("deal_task_agent")

        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
                request_params={"parallel_tool_calls": True},
            ),
            output_schema=DealTaskSyncResult,
            description="Deal Task Agent for post-meeting task synchronization",
            tools=self._get_tools(),
            tool_call_limit=80,
            **get_agno_datetime_settings(self.context.metadata),
            debug_mode=config.debug_mode,
            system_message=get_deal_task_system_prompt(self.org_name),
            telemetry=False,
        )

    def _get_tools(self) -> list[Callable]:
        """Return the central registry tool set for DealTaskAgent."""
        # Tool lookup stays dynamic by agent name to avoid duplicating the shared
        # tool access map when runtime capabilities change.
        return get_tools_for_agent("DealTaskAgent", self.context, lambda: None)

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish incremental progress only when a publisher is provided."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def sync_from_summary(self) -> DealTaskSyncResult:
        """Run task synchronization for the current deal after meeting analysis."""
        # Build a deterministic user-facing flow: list first, then reconcile action
        # items. This avoids creating tasks before we know current state.
        logger.info("DealTaskAgent syncing tasks for deal %s", self.deal_id)
        self._publish_progress("Analyzing meeting actions for deal tasks...", "analysis", 0.2)

        user_prompt = (
            f"Reconcile CRM tasks for deal {self.deal_id}. "
            "Use the latest meeting summary, deal memory, and existing deal tasks. "
            "Create concrete missing follow-ups, complete only explicitly finished internal tasks, "
            "and skip vague, duplicate, or read-only items."
        )

        response = await self.agent.arun(user_prompt)
        self.last_token_usage = extract_token_usage(response, agent_name="DealTaskAgent")

        content = response.content
        if isinstance(content, DealTaskSyncResult):
            return content
        if isinstance(content, dict):
            try:
                return DealTaskSyncResult.model_validate(content)
            except Exception:
                logger.warning(
                    "DealTaskAgent returned invalid dict for deal %s",
                    self.deal_id,
                    exc_info=True,
                )

        logger.warning(
            "DealTaskAgent returned non-structured response for deal %s: %.200s",
            self.deal_id,
            str(content),
        )
        return DealTaskSyncResult()


__all__ = ["DealTaskAgent"]
