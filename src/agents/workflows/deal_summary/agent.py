"""Deal summary agent for generating comprehensive deal summaries from all meeting transcripts.

This agent produces structured DealSummaryData output by analyzing ALL meetings
associated with a deal, not just a single meeting. The Celery worker handles
streaming progress updates to the client via Redis.
"""

from __future__ import annotations

from typing import cast

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.workflows.deal_summary.models import DealSummaryData
from src.agents.workflows.deal_summary.prompts import DEAL_SUMMARY_SYSTEM_PROMPT
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class DealSummaryAgent:
    """Agent for generating comprehensive deal summaries.

    Uses Agno with structured output (response_model) for reliable JSON output.
    Analyzes all meeting transcripts for a deal to produce a holistic summary.
    """

    def __init__(self):
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
            response_model=DealSummaryData,
            description=DEAL_SUMMARY_SYSTEM_PROMPT,
        )

    async def generate(self, context_string: str) -> DealSummaryData:
        """Generate a comprehensive deal summary from all meeting transcripts.

        Args:
            context_string: The aggregated deal context including all meeting transcripts

        Returns:
            DealSummaryData with comprehensive deal summary fields
        """
        logger.info("DealSummaryAgent generating deal summary from aggregated context")
        response = await self.agent.arun(context_string)
        return cast(DealSummaryData, response.content)
