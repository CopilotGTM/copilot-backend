"""Stakeholder analysis agent for extracting insights from meeting transcripts.

This agent produces structured DealStakeholdersAnalysisData output by analyzing
meeting transcripts to understand all stakeholders' stance, decision power,
engagement level, motivations, and pain points in a single LLM call.
"""

from __future__ import annotations

from typing import cast

from agno.agent import Agent
from agno.models.azure import AzureOpenAI

from src.agents.workflows.stakeholder_analysis.models import DealStakeholdersAnalysisData
from src.agents.workflows.stakeholder_analysis.prompts import (
    STAKEHOLDER_BATCH_ANALYSIS_SYSTEM_PROMPT,
)
from src.infra.config.agent_config import agent_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class StakeholderAnalysisAgent:
    """Agent for analyzing all stakeholder insights from meeting transcripts.

    Uses Agno with structured output (response_model) for reliable JSON output.
    Analyzes all stakeholders for a deal in a single LLM call for efficiency.
    """

    def __init__(self):
        # Get model config
        config = agent_config.get_config("stakeholder_analysis_agent")

        self.agent = Agent(
            model=AzureOpenAI(id=config.model, api_version=config.api_version),
            response_model=DealStakeholdersAnalysisData,
            description=STAKEHOLDER_BATCH_ANALYSIS_SYSTEM_PROMPT,
            instructions=[
                "Analyze all stakeholders listed in the context from the meeting transcripts.",
                "For EACH stakeholder, extract evidence-based insights about stance, decision power, and engagement.",
                "Identify specific pain points and motivations mentioned by each stakeholder.",
                "Capture key terms and phrases each stakeholder uses frequently.",
                "Provide a comprehensive analysis summary for each stakeholder.",
                "Be conservative with assessments - use UNKNOWN when evidence is insufficient.",
                "Return analysis for ALL stakeholders provided, even if some have limited data.",
                "Use the stakeholder's email as the identifier in your response.",
            ],
        )

    async def generate(self, context_string: str) -> DealStakeholdersAnalysisData:
        """Generate stakeholder analysis for all stakeholders from the context string.

        Args:
            context_string: The deal context with all stakeholders and meeting transcripts

        Returns:
            DealStakeholdersAnalysisData with analysis for all stakeholders
        """
        logger.info("StakeholderAnalysisAgent generating batch analysis from context")
        response = await self.agent.arun(context_string)
        return cast(DealStakeholdersAnalysisData, response.content)
