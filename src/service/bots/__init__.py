"""Chat bot integration services.

This package contains services for integrating with various chat platforms:
- Teams: Microsoft Teams bot integration
- Slack: Slack bot integration (future)
- Zoom: Zoom chat integration (future)
"""

from .auth_state_service import (
    BOT_AUTH_STATE_PREFIX,
    STATE_TOKEN_TTL_SECONDS,
    BotAuthState,
    BotAuthStateService,
    get_bot_auth_state_service,
)
from .identity_service import ExternalIdentityService
from .teams_bot_service import TeamsBotService
from .teams_graph_client import (
    get_graph_access_token,
    get_master_secret,
)

__all__ = [
    # Auth state service
    "BotAuthState",
    "BotAuthStateService",
    "get_bot_auth_state_service",
    "BOT_AUTH_STATE_PREFIX",
    "STATE_TOKEN_TTL_SECONDS",
    # Identity service
    "ExternalIdentityService",
    # Teams bot service
    "TeamsBotService",
    # Graph API client
    "get_graph_access_token",
    "get_master_secret",
]
