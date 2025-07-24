import uuid
from uuid import UUID, uuid4
from typing import Optional
from datetime import datetime
from enum import Enum

from sqlmodel import SQLModel, Field
from pydantic import EmailStr
from pydantic import BaseModel


def utcnow() -> datetime:
    return datetime.utcnow()


class AdminLoginRequest(SQLModel):
    email: EmailStr
    password: str


class AdminPasswordChangeRequest(BaseModel):
    new_password: str
    confirm_password: Optional[str] = None
    session_token: Optional[str] = None

class AdminFirstTimePasswordChangeRequest(BaseModel):
    new_password: str
    confirm_password: str
    session_token: str

class AdminChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class AdminGlobalLogoutRequest(BaseModel):
    email: EmailStr

class AdminForgotPasswordRequest(BaseModel):
    email: EmailStr
    redirect_to: Optional[str] = None

class AdminRefreshTokenRequest(BaseModel):
    refresh_token: str


class AdminResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class AdminResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class AdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"


class AdminStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_SETUP = "pending_setup"


class SuperAdmin(SQLModel, table=True):
    __tablename__ = "super_admins"

    admin_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    # Supabase auth user ID (from auth.users table)
    supabase_user_id: str = Field(
        ...,
        unique=True,
        index=True,
        nullable=False,
    )
    email: EmailStr = Field(
        ...,
        unique=True,
        index=True,
        nullable=False,
    )
    first_name: str = Field(..., max_length=100)
    last_name: str = Field(..., max_length=100)

    # Status and role
    role: AdminRole = Field(default=AdminRole.SUPER_ADMIN)
    status: AdminStatus = Field(default=AdminStatus.PENDING_SETUP)

    # Timestamps
    last_login_at: Optional[datetime] = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # Email verification
    email_verified: bool = Field(default=False)


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
    email: EmailStr
    first_name: str
    last_name: str
    role: AdminRole
    state: str
    last_login_at: Optional[datetime] = None
    created_at: datetime
    email_verified: bool
    tokens: Tokens
    session_token: Optional[str] = None


class AdminRefreshTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
