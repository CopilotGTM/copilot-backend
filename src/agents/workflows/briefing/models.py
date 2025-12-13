"""Models for briefing agent input and output.

Contains structured schemas used by the briefing agent for LLM responses
and minimal input data for tool-based generation.
"""

from pydantic import BaseModel, Field


class EventDetails(BaseModel):
    """Event details for which to prepare the briefing.

    The agent must still fetch the full meeting data via tools as its first step.
    These details help the model orient and allow deterministic prep text.
    """

    title: str | None = Field(None, description="Event title")
    description: str | None = Field(None, description="Event description/agenda")
    start_at: str | None = Field(None, description="Event start datetime (ISO)")
    end_at: str | None = Field(None, description="Event end datetime (ISO)")
    organiser_name: str | None = Field(None, description="Organizer name")
    organiser_email: str | None = Field(None, description="Organizer email")
    attendee_count: int | None = Field(None, description="Number of attendees")
    attendees_preview: list[str] = Field(
        default_factory=list,
        description="Small preview list of attendee names/emails (keep short)",
    )


class BriefingInput(BaseModel):
    """Minimal input for BriefingAgent (tool-first).

    This input is intentionally small. The agent must use tools to fetch:
    - the target meeting details (first step)
    - prior meetings
    - emails
    - stakeholders (only if needed)
    """

    deal_id: str = Field(..., description="UUID of the deal")
    event_id: str = Field(..., description="UUID of the target meeting")
    current_datetime: str = Field(
        ...,
        description="Current datetime in ISO format (job execution time). Used for time-aware reasoning.",
    )

    # Lightweight interaction signals (avoid preloading full history)
    prior_meetings_count: int | None = Field(
        None,
        description=(
            "Count of prior (non-rejected) meetings associated with the deal before the current job execution time. "
            "Excludes the target meeting. Useful to infer first-call/discovery vs later-stage calls."
        ),
    )
    emails_exchanged_count: int | None = Field(
        None,
        description=(
            "Count of (non-rejected) deal-linked emails exchanged before the current job execution time. "
            "Used as a lightweight signal for relationship maturity and open loops."
        ),
    )

    # Small hints (do not duplicate large datasets here)
    deal_name: str | None = Field(None, description="Deal name")
    company_name: str | None = Field(None, description="Company name")
    event_details: EventDetails | None = Field(
        None, description="Event details for which to prepare briefing"
    )


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
