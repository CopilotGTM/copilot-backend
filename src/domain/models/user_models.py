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
    timezone: str | None = None
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


class RealtimeDevicePlatform(str, Enum):
    """Supported client platforms for realtime sessions."""

    ANDROID = "android"
    IOS = "ios"
    WEB = "web"


class RealtimeConversationMode(str, Enum):
    """Supported realtime conversation modes."""

    PUSH_TO_TALK = "push_to_talk"


class RealtimeTurnMode(str, Enum):
    """Resolved turn mode returned to the client."""

    PUSH_TO_TALK = "push_to_talk"


class RealtimeToolPolicy(str, Enum):
    """Backend policy for when voice responses must ground through tools."""

    GUARDED_AUTO = "guardrailed_auto"


class RealtimeControlMode(str, Enum):
    """Backend control ownership for a live realtime call."""

    BACKEND_SIDEBAND = "backend_sideband"


class UserRealtimeDeviceInfo(BaseModel):
    """Device details sent when bootstrapping a realtime session."""

    platform: RealtimeDevicePlatform
    app_version: str


class UserRealtimeSessionRequest(BaseModel):
    """Request model for bootstrapping a realtime session."""

    device: UserRealtimeDeviceInfo
    conversation_mode: RealtimeConversationMode
    chat_session_id: str | None = None


class UserRealtimeRuntimeConfig(BaseModel):
    """Server-authored runtime contract for the voice client."""

    chat_session_id: str
    control_mode: RealtimeControlMode
    tool_policy: RealtimeToolPolicy
    allowed_tools: list[str]
    session_update: dict[str, Any]
    issued_at: datetime
    expires_at: datetime
    signature: str


class UserRealtimeSessionResponse(BaseModel):
    """Response model for a bootstrapped realtime session."""

    session_id: str
    ephemeral_key: str
    endpoint: str
    model: str
    voice: str
    expires_at: datetime
    turn_mode: RealtimeTurnMode
    chat_session_id: str
    runtime_config: UserRealtimeRuntimeConfig


class UserRealtimeCallAttachRequest(BaseModel):
    """Request model for attaching backend sideband control to a live call."""

    session_id: str
    call_id: str
    chat_session_id: str
    runtime_config: UserRealtimeRuntimeConfig


class UserRealtimeCallAttachResponse(BaseModel):
    """Response model for a successfully attached backend sideband call."""

    call_id: str
    chat_session_id: str
    control_mode: RealtimeControlMode
    status: str
