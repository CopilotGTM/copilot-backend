"""Models for summary agent input and output.

Contains structured schemas used by the summary agent for LLM responses
and minimal input data for tool-based generation.
"""

from pydantic import BaseModel, Field


class SummaryInput(BaseModel):
    """Minimal input data for SummaryAgent.

    Instead of passing all data pre-loaded, we pass minimal context
    and let the agent use tools to gather what it needs.
    """

    # Target meeting
    event_id: str = Field(..., description="UUID of the meeting to summarize")
    deal_id: str | None = Field(
        None,
        description=(
            "UUID of the associated deal, if known. Helps the agent fetch deal context more easily."
        ),
    )

    # Optional: current time for temporal grounding (best-effort)
    current_datetime: str | None = Field(
        None,
        description="ISO datetime at job execution time (optional, for context only)",
    )


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
