"""Stakeholder Research Agent — researches individual stakeholders via Apollo."""

from src.agents.workflows.stakeholder_research.agent import StakeholderResearchAgent
from src.agents.workflows.stakeholder_research.models import StakeholderResearchResult

__all__ = [
    "StakeholderResearchAgent",
    "StakeholderResearchResult",
]
