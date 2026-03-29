"""Models for summary agent input and output."""

from pydantic import BaseModel, Field


class SummaryAttendee(BaseModel):
    """Attendee roster entry passed into the meeting summary workflow."""

    email: str = Field(..., description="Attendee email address used for identity grounding")
    name: str | None = Field(
        None,
        description="Attendee display name from the calendar provider, if available",
    )
    response_status: str = Field(
        ...,
        description="Normalized RSVP status such as accepted, tentative, declined, or none",
    )
    is_organiser: bool = Field(
        False,
        description="Whether this attendee is the organiser of the meeting",
    )


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

    attendees: list[SummaryAttendee] = Field(
        ...,
        description=(
            "Canonical meeting roster used for identity grounding. This is the invite/RSVP list, "
            "not proof of live participation."
        ),
    )
    internal_domains: list[str] = Field(
        ...,
        description=(
            "Authoritative email domains for the seller organization, used to classify internal "
            "vs buyer-side attendees."
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
