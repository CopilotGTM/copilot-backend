"""Chat bot API routers.

This package contains API endpoints for various chat bot integrations:
- /bots/auth/* - Shared authentication endpoints (callback, status)
- /bots/teams/* - Teams-specific endpoints (webhook, health)
- /bots/meeting/* - Meeting bot callback endpoints (transcript, status)
- /bots/slack/* - Slack-specific endpoints (events webhook)
"""

from fastapi import APIRouter

from .auth_callback_handler import router as auth_router
from .slack_webhook_handler import router as slack_router
from .teams_webhook_handler import router as teams_router

bots_router = APIRouter()
bots_router.include_router(auth_router, prefix="/auth")
bots_router.include_router(teams_router, prefix="/teams")
bots_router.include_router(slack_router, prefix="/slack")

__all__ = ["bots_router"]
