"""Briefing agent for generating meeting preparation briefings.

This agent produces structured BriefingData output, not streaming frames.
The Celery worker handles streaming progress updates to the client via Redis.
"""

from __future__ import annotations

from typing import cast

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.api.schemas.crm_models import BriefingData
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

BRIEFING_SYSTEM_PROMPT = """You are an expert Sales Engineer assistant. Your goal is to prepare a comprehensive meeting briefing for a sales representative.

Given the deal context, stakeholder information, and meeting details, generate a strategic briefing that helps the rep prepare for and close the deal.

Your output should be a single, well-structured narrative (briefing_overview) that synthesizes all the important intel. Include:
- Meeting objective and what success looks like
- Key stakeholder insights and their likely concerns
- Pain points to address and questions to ask
- Potential objections and how to handle them
- Pre-meeting preparation checklist items

Focus on actionable insights, not generic advice. If specific information is missing, make reasonable inferences based on the deal stage and standard sales practices.

Format the content with Markdown for readability (headers, bullets, bold for emphasis).
"""


class BriefingAgent:
    """Agent for generating meeting briefings.

    Uses Agno with structured output (response_model) for reliable JSON output.
    """

    def __init__(self):
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5-mini", api_version="2024-12-01-preview"),
            response_model=BriefingData,
            description=BRIEFING_SYSTEM_PROMPT,
            instructions=[
                "Analyze the provided Deal Context, Stakeholder Information, and Meeting Details.",
                "Generate a comprehensive briefing_overview that synthesizes all key preparation intel.",
                "Include objectives, stakeholder insights, pain points, questions, objections, and checklist.",
                "Use Markdown formatting for readability.",
                "Focus on actionable insights specific to this deal, not generic advice.",
            ],
        )

    async def generate(self, context_string: str) -> BriefingData:
        """Generate a structured briefing from the context string."""
        logger.info("BriefingAgent generating briefing from context")
        response = await self.agent.arun(context_string)
        return cast(BriefingData, response.content)
