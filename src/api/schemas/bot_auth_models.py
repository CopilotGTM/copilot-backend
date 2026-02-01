"""Pydantic schemas for bot authentication endpoints."""

from pydantic import BaseModel, Field


class BotAuthCallbackRequest(BaseModel):
    """Request body for bot auth callback."""

    access_token: str = Field(..., description="Supabase access token from frontend login")
    state: str = Field(..., description="State token from login URL")


class BotAuthCallbackResponse(BaseModel):
    """Response for successful bot auth callback."""

    success: bool = True
    message: str = "Account linked successfully"
    user_email: str | None = None
    provider: str | None = None


class BotAuthStatusResponse(BaseModel):
    """Response for bot auth status check."""

    is_linked: bool
    user_email: str | None = None
    linked_at: str | None = None
    provider: str | None = None


class BotAuthValidateStateResponse(BaseModel):
    """Response for state token validation check."""

    valid: bool
    provider: str | None = None
    expires_in_seconds: int = 0


class BotAuthErrorResponse(BaseModel):
    """Error response for bot auth endpoints."""

    success: bool = False
    error: str
    error_code: str | None = None
