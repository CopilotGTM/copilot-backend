"""Summary agent for generating meeting summaries from transcripts.

This agent produces structured SummaryData output, not streaming frames.
The Celery worker handles streaming progress updates to the client via Redis.
"""

from __future__ import annotations

from typing import cast

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.workflows.summary.models import SummaryData
from src.agents.workflows.summary.prompts import SUMMARY_SYSTEM_PROMPT
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class SummaryAgent:
    """Agent for generating meeting summaries from transcripts.

    Uses Agno with structured output (response_model) for reliable JSON output.
    The output contains a single meeting_summary field with the full narrative.
    """

    def __init__(self):
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
            response_model=SummaryData,
            description=SUMMARY_SYSTEM_PROMPT,
        )

    async def generate(self, context_string: str) -> SummaryData:
        """Generate a structured summary from the transcript and context.

        Args:
            context_string: The meeting transcript with deal context

        Returns:
            SummaryData with meeting_summary field containing the full narrative
        """
        logger.info("SummaryAgent generating summary from transcript")
        response = await self.agent.arun(context_string)
        return cast(SummaryData, response.content)
