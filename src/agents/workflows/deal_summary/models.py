"""Models for deal summary agent output.

Contains structured output schemas used by the deal summary agent for LLM responses.
"""

from pydantic import BaseModel, Field


class DealSummaryData(BaseModel):
    """Generated deal summary data.

    Aggregates insights from all meetings associated with a deal,
    providing a comprehensive overview of the deal's history and status.
    """

    deal_summary: str = Field(
        ...,
        description="Comprehensive narrative summary of the entire deal, synthesizing all meeting discussions, key decisions, stakeholder dynamics, and deal progression.",
    )
