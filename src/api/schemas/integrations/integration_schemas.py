"""
Pydantic schemas for integration API endpoints.

These schemas define the request/response structures for:
- OAuth flow initiation and callbacks
- Integration CRUD operations
- Sync operations and history
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
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
    created_by_admin: bool = Field(description="Whether this integration was created by an admin")
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
    created_by_admin: bool
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
    connected_integrations: list[IntegrationResponse] = Field(
        default_factory=list,
        description="List of user's connected instances of this integration type",
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
