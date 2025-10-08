from uuid import UUID
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, EmailStr, Field

from src.domain.models.master_tables import UserAppRole
from src.domain.models.admin_models import AdminLoginState
from enum import Enum


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

    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[UserAppRole] = None
    state: UserLoginState
    created_at: Optional[datetime] = None
    email_verified: Optional[bool] = None
    last_login_at: Optional[datetime] = None
    tokens: Optional[Dict[str, Any]] = None
    password_reset_link: Optional[str] = None


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
    confirm_password: Optional[str] = None
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
    email: Optional[EmailStr] = None
