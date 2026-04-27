"""Chat bot API routers.

This package contains API endpoints for various chat bot integrations:
- /bots/auth/* - Shared authentication endpoints (callback, status)
- /bots/teams/* - Teams-specific endpoints (webhook, health)
- /bots/slack/* - Slack-specific endpoints (events webhook)
- /bots/google-chat/* - Google Chat interaction endpoint

Meeting-recording provider webhooks live under `src.api.routers.meeting_bot`
because they are backend-to-backend callbacks, not user auth/linking routes.
"""

from fastapi import APIRouter

from .auth_callback_handler import router as auth_router
from .google_chat_webhook_handler import router as google_chat_router
from .slack_webhook_handler import router as slack_router
from .teams_webhook_handler import router as teams_router
from .whatsapp_webhook_handler import router as whatsapp_router

bots_router = APIRouter()
bots_router.include_router(auth_router, prefix="/auth")
bots_router.include_router(teams_router, prefix="/teams")
bots_router.include_router(slack_router, prefix="/slack")
bots_router.include_router(whatsapp_router, prefix="/whatsapp")
bots_router.include_router(google_chat_router, prefix="/google-chat")

__all__ = ["bots_router"]
