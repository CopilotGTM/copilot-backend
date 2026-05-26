"""
Pydantic schemas for integration API endpoints.

These schemas define the request/response structures for:
- OAuth flow initiation and callbacks
- Integration CRUD operations
- Sync operations and history
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
)

# ==================== OAuth Schemas ====================


class OAuthInitiateRequest(BaseModel):
    """Request to initiate OAuth flow for an integration."""

    integration_type: IntegrationType = Field(
        ...,
        description="Type of integration to connect (e.g., HUBSPOT, SLACK, GOOGLE_DRIVE)",
    )
    integration_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Optional user-defined name for this integration (auto-generated if not provided)",
    )
    oauth_base_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Optional provider OAuth login host. Salesforce defaults to login.salesforce.com.",
    )

    @field_validator("oauth_base_url", mode="before")
    @classmethod
    def normalize_empty_oauth_base_url(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_oauth_base_url_provider(self) -> "OAuthInitiateRequest":
        if self.oauth_base_url and self.integration_type != IntegrationType.SALESFORCE:
            raise ValueError("oauth_base_url is only supported for Salesforce integrations")
        return self


class OAuthInitiateResponse(BaseModel):
    """Response containing Nango Connect Session token.

    New Flow (Post July 31, 2025):
    - Frontend receives session_token
    - Frontend uses Nango Connect UI with this token
    - After authorization, Nango sends webhook with connection_id
    """

    session_token: str = Field(
        ...,
        description="Nango Connect Session token for frontend to use with Nango Connect UI",
    )
    integration_type: IntegrationType = Field(..., description="Integration type being connected")
    expires_at: str | None = Field(
        default=None, description="When the session token expires (ISO 8601 format)"
    )


class OAuthCallbackRequest(BaseModel):
    """Query parameters received in OAuth callback."""

    code: str = Field(..., description="Authorization code from OAuth provider")
    state: str | None = Field(default=None, description="State token for validation")
    connection_id: str | None = Field(
        default=None, description="Connection identifier (may be in state or separate)"
    )


# ==================== Integration Schemas ====================


class IntegrationBase(BaseModel):
    """Base fields for integration."""

    integration_type: IntegrationType
    name: str = Field(min_length=1, max_length=100)
    sync_frequency: str = Field(
        default="daily",
        pattern="^(hourly|daily|weekly|manual)$",
        description="How often to sync: hourly, daily, weekly, or manual",
    )
    auto_sync_enabled: bool = Field(
        default=True, description="Whether automatic syncing is enabled"
    )


class IntegrationCreate(IntegrationBase):
    """Schema for creating a new integration (internal use after OAuth)."""

    connection_id: str
    scopes: list[str] | None = None
    connection_metadata: dict[str, Any] | None = None


class IntegrationUpdate(BaseModel):
    """Schema for updating integration settings."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    sync_frequency: str | None = Field(default=None, pattern="^(hourly|daily|weekly|manual)$")
    auto_sync_enabled: bool | None = None


class IntegrationResponse(IntegrationBase):
    """Response schema for integration details."""

    integration_id: UUID
    organisation_id: UUID
    created_by_user_id: UUID = Field(description="User who created this integration")
    integration_email: str | None = Field(
        default=None,
        description="Email/username of the connected account (e.g., 'sales@acme.com' for HubSpot)",
    )
    integration_scope: IntegrationScope = Field(
        description="Access scope: ORGANIZATION (all users) or USER (creator only)"
    )
    is_active: bool
    last_synced_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    access_token_expires_at: datetime | None = None
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    requires_reauth: bool = Field(
        default=False,
        description="True when the integration has expired and must be reconnected.",
    )
    action_required: str = Field(
        default="none",
        description="UI action state: none, reauth_expired, additional_permissions",
    )
    action_message: str | None = Field(
        default=None,
        description="User-facing guidance for reconnecting this integration",
    )
    missing_scopes: list[str] = Field(
        default_factory=list,
        description="Provider scopes currently required but missing from the stored grant",
    )
    missing_scope_labels: list[str] = Field(
        default_factory=list,
        description="Readable names for missing provider scopes",
    )
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class IntegrationListResponse(BaseModel):
    """Response for listing integrations."""

    integrations: list[IntegrationResponse]
    total: int


class IntegrationStatusResponse(BaseModel):
    """Lightweight response for integration status checks."""

    integration_id: UUID
    name: str
    integration_type: IntegrationType
    integration_scope: IntegrationScope
    created_by_user_id: UUID
    integration_email: str | None = Field(
        default=None, description="Email/username of the connected account"
    )
    is_active: bool
    last_synced_at: datetime | None = None
    last_sync_status: str | None = None
    needs_reauthorization: bool = Field(
        default=False, description="True if token has expired and requires re-auth"
    )


# ==================== Sync Schemas ====================


class SyncTriggerRequest(BaseModel):
    """Request to manually trigger a sync operation."""

    full_sync: bool = Field(
        default=False, description="If true, performs full sync; otherwise incremental"
    )


class SyncTriggerResponse(BaseModel):
    """Response when sync is triggered."""

    sync_id: UUID = Field(..., description="ID of the sync job that was created")
    integration_id: UUID
    status: str = Field(default="queued", description="Initial status of the sync job")
    message: str = Field(default="Sync job queued successfully")


class SyncHistoryResponse(BaseModel):
    """Response for a single sync history entry."""

    sync_id: UUID
    integration_id: UUID
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    records_processed: int = 0
    records_failed: int = 0
    error_details: dict[str, Any] | None = None
    sync_metadata: dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True


class SyncHistoryListResponse(BaseModel):
    """Response for listing sync history."""

    history: list[SyncHistoryResponse]
    total: int
    integration_id: UUID


# ==================== Available Integrations ====================


class LinkedChannel(BaseModel):
    """Schema for a channel linked to a deal."""

    channel_id: UUID = Field(..., description="Internal Channel UUID")
    external_channel_id: str = Field(..., description="Provider's Channel ID")
    name: str = Field(..., description="Channel Name")
    type: str = Field(..., description="Channel Type")
    integration_id: UUID = Field(..., description="Integration ID")
    integration_name: str = Field(..., description="Name of the integration connection")
    parent_name: str | None = Field(default=None, description="Parent Name (e.g. Team Name)")


class IntegrationAdminPrerequisite(BaseModel):
    """Admin action required before a provider's normal user OAuth flow."""

    type: Literal["install_url"] = Field(..., description="Prerequisite action type")
    required_before_connect: bool = Field(
        ..., description="Whether this action must happen before the normal connect flow"
    )
    url: str = Field(..., description="Admin URL for completing the prerequisite action")
    label: str = Field(..., description="Display label for the prerequisite action")


class AvailableIntegration(BaseModel):
    """Information about an available integration type."""

    integration_type: IntegrationType
    name: str = Field(..., description="Display name for the integration")
    description: str = Field(..., description="Brief description of what the integration does")
    logo_url: str | None = Field(default=None, description="URL to integration provider's logo")
    category: str = Field(..., description="Category slug: collaboration, crm, storage, etc.")
    category_name: str = Field(..., description="Human-readable category name for display")
    availability_status: str = Field(
        ...,
        description="Integration availability: available, coming_soon, beta, deprecated",
    )
    is_connected: bool = Field(
        default=False, description="Whether user has already connected this integration"
    )
    has_action_required: bool = Field(
        default=False,
        description="Whether any visible integration instance needs the user to reconnect.",
    )
    connected_integrations: list[IntegrationResponse] = Field(
        default_factory=list,
        description="List of user's connected instances of this integration type",
    )
    action_required_integrations: list[IntegrationResponse] = Field(
        default_factory=list,
        description="Inactive or reconnect-required instances that need user attention",
    )
    linked_channels: list[LinkedChannel] = Field(
        default_factory=list,
        description="List of channels linked to the current deal context",
    )
    admin_prerequisite: IntegrationAdminPrerequisite | None = Field(
        default=None,
        description="Optional admin prerequisite to complete before starting provider OAuth",
    )


class AvailableIntegrationsResponse(BaseModel):
    """Response listing all available integration types with connection status."""

    integrations: list[AvailableIntegration]
    total_available: int = Field(..., description="Total number of available integration types")
    total_connected: int = Field(
        ..., description="Total number of connected integrations across all types"
    )


# ==================== Error Schemas ====================


class IntegrationError(BaseModel):
    """Standard error response for integration endpoints."""

    error: str = Field(..., description="Error type or code")
    message: str = Field(..., description="Human-readable error message")
    details: dict[str, Any] | None = Field(default=None, description="Additional error details")


# ==================== Channel Schemas ====================


class ChannelMember(BaseModel):
    """Schema for a channel member."""

    name: str | None = Field(default=None, description="Member display name")
    email: str | None = Field(default=None, description="Member email address")
    role: str | None = Field(default=None, description="Role in channel (owner, admin, member)")


class ChannelItem(BaseModel):
    """Schema for a messaging channel/team with detailed information for selection."""

    id: str = Field(..., description="Channel ID or Team ID")
    name: str = Field(..., description="Channel name or Team name")
    integration_id: UUID = Field(..., description="Integration ID to use when linking this channel")
    type: str = Field(..., description="Type of channel (public, private, group, meeting, etc.)")
    type_label: str = Field(
        ...,
        description="Human-readable channel type label (e.g., 'Public Channel', 'Meeting Chat')",
    )
    parent_id: str | None = Field(
        default=None, description="Parent ID (e.g., Team ID for a channel)"
    )
    parent_name: str | None = Field(default=None, description="Parent name (e.g., Team name)")
    description: str | None = Field(default=None, description="Channel description or topic")
    member_count: int = Field(default=0, description="Number of members in the channel")
    members: list[ChannelMember] = Field(
        default_factory=list,
        description="List of channel members (limited to first 10 for display)",
    )
    last_activity_at: datetime | None = Field(
        default=None, description="Timestamp of the most recent message"
    )


class ChannelListResponse(BaseModel):
    """Response for listing channels."""

    channels: list[ChannelItem]
    total: int


class ChannelLinkRequest(BaseModel):
    """Request schema for linking a channel to a deal."""

    id: str = Field(..., description="External channel ID from the provider")
    name: str = Field(..., description="Channel Name")
    type: str = Field(..., description="Channel Type")
    integration_id: UUID | None = Field(
        default=None,
        description="Integration ID to use for this channel when the client can provide it",
    )
    parent_id: str | None = Field(default=None, description="Parent ID (e.g. Team ID)")
    parent_name: str | None = Field(default=None, description="Parent Name")


class ChannelLinkResponse(BaseModel):
    """Response schema for linking a channel to a deal."""

    channel_id: UUID = Field(..., description="Internal Channel UUID")
    deal_id: UUID = Field(..., description="Deal UUID")
    external_channel_id: str = Field(..., description="Provider's Channel ID")
    name: str = Field(..., description="Channel Name")
    type: str = Field(..., description="Channel Type")
    integration_id: UUID = Field(..., description="Integration ID")


class EmptyResponse(BaseModel):
    """Empty response model for endpoints that don't return data."""

    pass


class RefreshChannelsResponse(BaseModel):
    """Response for channel refresh request."""

    task_id: str = Field(..., description="Celery task ID for tracking the refresh job")
    integration_id: UUID = Field(..., description="Integration UUID")
    message: str = Field(..., description="Status message")

    class Config:
        from_attributes = True
