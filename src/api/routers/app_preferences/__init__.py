from src.api.routers.app_preferences.agent_actions_handler import (
    agent_action_approval_router,
)
from src.api.routers.app_preferences.calendar_sender_handler import (
    calendar_sender_preferences_router,
)
from src.api.routers.app_preferences.crm_list_handler import crm_list_preferences_router
from src.api.routers.app_preferences.meeting_recording_policy_handler import (
    meeting_recording_policy_router,
)
from src.api.routers.app_preferences.navigation_handler import navigation_preferences_router

__all__ = [
    "agent_action_approval_router",
    "calendar_sender_preferences_router",
    "crm_list_preferences_router",
    "meeting_recording_policy_router",
    "navigation_preferences_router",
]
