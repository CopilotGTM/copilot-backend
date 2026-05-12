"""Calendar invite creation service package for user-owned calendar writes.

Callers should use this package when handling user-initiated invite actions.
The public exports are intentionally narrow so API/tool code can construct and
read request/result models without touching transport details.
"""

from .attendee_resolver import (
    AttendeeCandidate,
    AttendeeResolutionResult,
    CalendarInviteAttendeeResolver,
)
from .models import (
    CalendarInviteAttendee,
    CalendarInviteCreateRequest,
    CalendarInviteCreateResult,
    CalendarInviteError,
)

__all__ = [
    "AttendeeCandidate",
    "AttendeeResolutionResult",
    "CalendarInviteAttendee",
    "CalendarInviteAttendeeResolver",
    "CalendarInviteCreateRequest",
    "CalendarInviteCreateResult",
    "CalendarInviteError",
]
