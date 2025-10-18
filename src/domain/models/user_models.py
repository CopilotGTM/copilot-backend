from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, EmailStr
from src.domain.models.master_tables import UserAppRole


class UserLoginState(str, Enum):
    """Enum for user login states."""

    ACTIVE = "ACTIVE"
    PENDING_SETUP = "PASSWORD_CHANGE_REQUIRED"


class UserLoginRequest(BaseModel):
    """Request model for user login."""

    email: EmailStr
    password: str


class UserLoginResponse(BaseModel):
    """Response model for successful user login."""

    email: EmailStr | None = None
    first_name: str | None = None
    last_name: str | None = None
    role: UserAppRole | None = None
    state: UserLoginState
    created_at: datetime | None = None
    email_verified: bool | None = None
    last_login_at: datetime | None = None
    tokens: dict[str, Any] | None = None
    password_reset_link: str | None = None


class UserChangePasswordRequest(BaseModel):
    """Request model for changing a user's password."""

    current_password: str
    new_password: str


class UserForgotPasswordRequest(BaseModel):
    """Request model for initiating a password reset."""

    email: EmailStr


class UserResetPasswordRequest(BaseModel):
    """Request model for resetting a user's password with a token."""

    token_hash: str
    new_password: str
    confirm_password: str | None = None
    action: str  # 'force_change' or 'forgot'


class UserRefreshTokenRequest(BaseModel):
    """Request model for refreshing an access token."""

    refresh_token: str


class UserRefreshTokenResponse(BaseModel):
    """Response model for a successful token refresh."""

    access_token: str
    refresh_token: str


class UserLogoutRequest(BaseModel):
    """Request model for user logout."""

    global_logout: bool = False
    email: EmailStr | None = None
