"""Chat bot API routers.

This package contains API endpoints for various chat bot integrations:
- /bots/auth/* - Shared authentication endpoints (callback, status)
- /bots/teams/* - Teams-specific endpoints (webhook, health)
- /bots/slack/* - Slack-specific endpoints (future)
"""

from fastapi import APIRouter

from .auth_callback_handler import router as auth_router
from .teams_webhook_handler import router as teams_router

bots_router = APIRouter()
bots_router.include_router(auth_router, prefix="/auth")
bots_router.include_router(teams_router, prefix="/teams")

__all__ = ["bots_router"]
