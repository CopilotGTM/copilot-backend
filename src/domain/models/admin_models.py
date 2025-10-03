import uuid
from uuid import UUID, uuid4
from typing import Optional, Literal
from datetime import datetime, timezone
from enum import Enum

from sqlmodel import SQLModel, Field
from pydantic import EmailStr
from pydantic import BaseModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdminLoginRequest(SQLModel):
    email: EmailStr
    password: str


class AdminPasswordChangeRequest(BaseModel):
    new_password: str
    confirm_password: Optional[str] = None
    session_token: Optional[str] = None

class AdminChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class AdminLogoutRequest(BaseModel):
    """
    Unified logout request that handles both regular and global logout.
    
    - global: If True, logout from all sessions globally. If False, logout from current session only.
    - email: Required only for global logout (to specify which user to logout)
    """
    global_logout: bool = False
    email: Optional[EmailStr] = None

class AdminForgotPasswordRequest(BaseModel):
    email: EmailStr

class AdminRefreshTokenRequest(BaseModel):
    refresh_token: str


class PasswordResetAction(str, Enum):
    """Enum for password reset actions"""
    FORGOT = "forgot"
    FORCE_CHANGE = "force_change"


# Unified Password Management Request
class AdminResetPasswordRequest(BaseModel):
    """
    Unified password management request that handles password reset operations.
    
    The action field determines which password operation to perform:
    - 'forgot': Request password reset email (requires email)
    - 'force_change': First-time password change (requires session_token, new_password)
    """
    action: PasswordResetAction = PasswordResetAction.FORGOT
    
    # Common fields
    new_password: str  # For force_change action
    confirm_password: str  # For force_change action
    
    # Action-specific fields
    token_hash: str  # For force_change action

class AdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"


class AdminStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_SETUP = "pending_setup"

class AdminCreateRequest(SQLModel):
    email: EmailStr
    first_name: str
    last_name: str
    role: AdminRole = AdminRole.SUPER_ADMIN


class AdminUpdateRequest(SQLModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: Optional[AdminStatus] = None

class Tokens(SQLModel):
    access_token: str
    refresh_token: str

class AdminLoginState(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    PENDING_SETUP = "PASSWORD_CHANGE_REQUIRED"

class AdminLoginResponse(SQLModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[AdminRole] = None
    state: str
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    email_verified: Optional[bool] = None
    tokens: Optional[Tokens] = None
    password_reset_link: Optional[str] = None  # For PENDING_SETUP state


class AdminRefreshTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
