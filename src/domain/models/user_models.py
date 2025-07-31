from uuid import UUID
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, EmailStr, Field

from src.domain.models.master_tables import UserAppRole
from src.domain.models.admin_models import AdminLoginState

class UserLoginRequest(BaseModel):
    """Request model for user login."""
    email: EmailStr
    password: str

class UserLoginResponse(BaseModel):
    """Response model for successful user login."""
    email: EmailStr
    first_name: str
    last_name: str
    role: UserAppRole
    state: AdminLoginState
    created_at: datetime
    email_verified: bool
    last_login_at: Optional[datetime]
    tokens: Dict[str, Any]

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
    action: str # 'force_change' or 'forgot'

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