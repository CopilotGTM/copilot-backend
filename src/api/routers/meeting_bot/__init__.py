"""Meeting bot API routers.

The `/users/meeting-bot/*` namespace is reserved for provider-to-backend webhook
traffic, so it stays separate from the user identity-linking routes under
`/users/bots/*`.
"""

from fastapi import APIRouter

from .recall_webhook_handler import router as recall_router

meeting_bot_router = APIRouter()
meeting_bot_router.include_router(recall_router, prefix="/recall")

__all__ = ["meeting_bot_router"]
