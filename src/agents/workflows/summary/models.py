"""Models for summary agent output.

Contains structured output schemas used by the summary agent for LLM responses.
"""

from pydantic import BaseModel, Field


class SummaryData(BaseModel):
    """Generated meeting summary data.

    Contains a single narrative field with the full summary content.
    Future versions may add structured sections (attendees, decisions, etc.)
    but for v1.0 we ship only the primary narrative.
    """

    meeting_summary: str = Field(
        ...,
        description="Transcript-backed recap of what actually happened, including decisions and context.",
    )
