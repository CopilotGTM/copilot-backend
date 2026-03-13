"""TranscriptCompressionAgent — lightweight sub-agent that compresses transcripts.

No tools, no DB access. Receives formatted transcript text, returns condensed markdown.
Designed to run inside the get_compressed_transcript tool.
"""

from __future__ import annotations

import logging

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI

from src.agents.sub_agents.transcript_compression.prompts import (
    get_compression_system_prompt,
)
from src.infra.config.agent_config import agent_config

logger = logging.getLogger(__name__)


class TranscriptCompressionAgent:
    """Compresses a single meeting transcript into speaker-attributed key points.

    Stateless, no tools, no DB access. Created per-call inside the tool wrapper.
    """

    def __init__(self, org_name: str = "Our Organization"):
        config = agent_config.get_config("transcript_compression_agent")

        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
                retries=config.retries,
                delay_between_retries=1,
                exponential_backoff=True,
            ),
            description=get_compression_system_prompt(org_name),
            markdown=True,
            debug_mode=config.debug_mode,
            telemetry=False,
        )

    async def compress(
        self,
        meeting_title: str,
        meeting_date: str,
        segments_text: str,
    ) -> str:
        """Compress a formatted transcript into speaker-attributed key points.

        Args:
            meeting_title: Title of the meeting (e.g., "Q3 Planning Review")
            meeting_date: Date string (e.g., "2025-01-15")
            segments_text: Pre-formatted transcript segments with speaker labels

        Returns:
            Condensed markdown string with Key Dialogue, Decisions, Action Items, Unresolved.
        """
        user_prompt = (
            f"## Meeting: {meeting_title} ({meeting_date})\n\n## Transcript\n\n{segments_text}"
        )

        logger.info(
            "TranscriptCompressionAgent compressing transcript for '%s' (%s), "
            "input length: %d chars",
            meeting_title,
            meeting_date,
            len(segments_text),
        )

        response = await self.agent.arun(user_prompt)
        content = response.content

        result = content if isinstance(content, str) else str(content)

        logger.info(
            "TranscriptCompressionAgent compressed '%s': %d → %d chars",
            meeting_title,
            len(segments_text),
            len(result),
        )

        return result
