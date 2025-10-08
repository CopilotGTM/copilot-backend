"""
Pydantic schemas for integration API endpoints.

These schemas define the request/response structures for:
- OAuth flow initiation and callbacks
- Integration CRUD operations
- Sync operations and history
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field, field_validator

from src.domain.models.organisation_tables.knowledge_models import IntegrationType


# ==================== OAuth Schemas ====================


class OAuthInitiateRequest(BaseModel):
    """Request to initiate OAuth flow for an integration."""

    integration_type: IntegrationType = Field(
        ...,
        description="Type of integration to connect (e.g., HUBSPOT, SLACK, GOOGLE_DRIVE)",
    )
    integration_name: Optional[str] = Field(
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
    integration_type: IntegrationType = Field(
        ..., description="Integration type being connected"
    )
    expires_at: Optional[str] = Field(
        default=None, description="When the session token expires (ISO 8601 format)"
    )


class OAuthCallbackRequest(BaseModel):
    """Query parameters received in OAuth callback."""

    code: str = Field(..., description="Authorization code from OAuth provider")
    state: Optional[str] = Field(default=None, description="State token for validation")
    connection_id: Optional[str] = Field(
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
    scopes: Optional[List[str]] = None
    connection_metadata: Optional[Dict[str, Any]] = None


class IntegrationUpdate(BaseModel):
    """Schema for updating integration settings."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    sync_frequency: Optional[str] = Field(
        default=None, pattern="^(hourly|daily|weekly|manual)$"
    )
    auto_sync_enabled: Optional[bool] = None


class IntegrationResponse(IntegrationBase):
    """Response schema for integration details."""

    integration_id: UUID
    organisation_id: UUID
    is_active: bool
    last_synced_at: Optional[datetime] = None
    last_sync_status: Optional[str] = None
    last_sync_error: Optional[str] = None
    access_token_expires_at: Optional[datetime] = None
    connection_metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class IntegrationListResponse(BaseModel):
    """Response for listing integrations."""

    integrations: List[IntegrationResponse]
    total: int


class IntegrationStatusResponse(BaseModel):
    """Lightweight response for integration status checks."""

    integration_id: UUID
    name: str
    integration_type: IntegrationType
    is_active: bool
    last_synced_at: Optional[datetime] = None
    last_sync_status: Optional[str] = None
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
    completed_at: Optional[datetime] = None
    status: str
    records_processed: int = 0
    records_failed: int = 0
    error_details: Optional[Dict[str, Any]] = None
    sync_metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True


class SyncHistoryListResponse(BaseModel):
    """Response for listing sync history."""

    history: List[SyncHistoryResponse]
    total: int
    integration_id: UUID


# ==================== Available Integrations ====================


class AvailableIntegration(BaseModel):
    """Information about an available integration type."""

    integration_type: IntegrationType
    name: str = Field(..., description="Display name for the integration")
    description: str = Field(
        ..., description="Brief description of what the integration does"
    )
    logo_url: Optional[str] = Field(
        default=None, description="URL to integration provider's logo"
    )
    category: str = Field(
        ..., description="Category slug: collaboration, crm, storage, etc."
    )
    category_name: str = Field(
        ..., description="Human-readable category name for display"
    )
    availability_status: str = Field(
        ...,
        description="Integration availability: available, coming_soon, beta, deprecated",
    )
    is_connected: bool = Field(
        default=False, description="Whether user has already connected this integration"
    )
    connected_integrations: List[IntegrationResponse] = Field(
        default_factory=list,
        description="List of user's connected instances of this integration type",
    )


class AvailableIntegrationsResponse(BaseModel):
    """Response listing all available integration types with connection status."""

    integrations: List[AvailableIntegration]
    total_available: int = Field(
        ..., description="Total number of available integration types"
    )
    total_connected: int = Field(
        ..., description="Total number of connected integrations across all types"
    )


# ==================== Error Schemas ====================


class IntegrationError(BaseModel):
    """Standard error response for integration endpoints."""

    error: str = Field(..., description="Error type or code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional error details"
    )
