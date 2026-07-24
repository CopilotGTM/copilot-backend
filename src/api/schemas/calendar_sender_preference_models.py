"""API contracts for a user's default outbound calendar account."""

from pydantic import BaseModel, ConfigDict, EmailStr


class CalendarSenderPreference(BaseModel):
    """Current preferred connected calendar, or null when inference should decide."""

    model_config = ConfigDict(extra="forbid")

    preferred_sender_email: EmailStr | None = None


class UpdateCalendarSenderPreferenceRequest(CalendarSenderPreference):
    """Replace or clear the user's preferred calendar sender."""
