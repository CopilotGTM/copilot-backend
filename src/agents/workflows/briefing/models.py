"""Models for briefing agent output.

Contains structured output schemas used by the briefing agent for LLM responses.
"""

from pydantic import BaseModel, Field


class BriefingData(BaseModel):
    """Generated meeting briefing data.

    Contains a single narrative field with the full briefing content.
    Future versions may add structured sections (objectives, pain_points, etc.)
    but for v1.0 we ship only the primary narrative.
    """

    briefing_overview: str = Field(
        ...,
        description="Multi-paragraph prep brief that synthesizes the most important intel before the call.",
    )
