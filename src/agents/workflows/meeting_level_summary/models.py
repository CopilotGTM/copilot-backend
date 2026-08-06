"""Models for meeting-level summary input."""

from pydantic import BaseModel, Field

from src.agents.workflows.summary.models import SummaryAttendee


class MeetingLevelSummaryInput(BaseModel):
    """Input for summaries where the meeting has no deal association."""

    event_id: str = Field(..., description="UUID of the meeting to summarize")
    deal_id: None = Field(None, description="Always null for meeting-level summaries")
    title: str | None = Field(None, description="Meeting title from the calendar event")
    start_at: str = Field(..., description="Meeting start timestamp")
    end_at: str = Field(..., description="Meeting end timestamp")
    attendees: list[SummaryAttendee] = Field(
        ...,
        description="Canonical calendar roster used for identity grounding",
    )
    internal_domains: list[str] = Field(
        ...,
        description="Authoritative seller-side domains for internal attendee classification",
    )
    transcript: str = Field(..., description="Full transcript text to summarize")
    current_datetime: str | None = Field(
        None,
        description="ISO datetime at job execution time (optional, for context only)",
    )
