"""Calendar write tools for agents."""

from src.agents.tools.calendar.send_calendar_invite import create_send_calendar_invite

# Export factory through this package namespace so registry imports remain stable
# even if the implementation module moves internally.
__all__ = ["create_send_calendar_invite"]
