from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from ..base_mixins import TimestampMixin

EMPTY_JSONB_SQL = "'{}'::jsonb"


class IntegrationType(str, Enum):
    # Collaboration & Communication
    SLACK = "slack"
    TEAMS = "teams"
    GOOGLE_CHAT = "google_chat"
    ZOOM = "zoom"
    GOOGLE_DRIVE = "google_drive"
    GOOGLE_CALENDAR = "google_calendar"
    OUTLOOK_CALENDAR = "outlook_calendar"
    OUTLOOK_EMAIL = "outlook_email"
    GMAIL = "gmail"
    SHAREPOINT = "sharepoint"
    ONEDRIVE = "onedrive"
    BOX = "box"
    DROPBOX = "dropbox"
    NOTION = "notion"
    CONFLUENCE = "confluence"
    MIRO = "miro"
    FIGMA = "figma"
    CALENDLY = "calendly"

    # Project Management
    JIRA = "jira"
    TRELLO = "trello"
    ASANA = "asana"
    MONDAY = "monday"

    # CRM & Customer Support
    ZENDESK = "zendesk"
    INTERCOM = "intercom"
    PIPEDRIVE = "pipedrive"
    ZOHO_CRM = "zoho_crm"

    # Sales & Presales Enablement
    SALESFORCE = "salesforce"
    HUBSPOT = "hubspot"
    SEISMIC = "seismic"
    HIGHSPOT = "highspot"
    SHOWPAD = "showpad"
    OUTREACH = "outreach"
    SALESLOFT = "salesloft"
    GONG = "gong"
    CHORUS = "chorus"


class IntegrationScope(str, Enum):
    """Defines the access scope for an integration connection."""

    ORGANIZATION = "organization"
    USER = "user"
    TEAM = "team"
    ROLE_BASED = "role_based"


class KnowledgeIntegration(TimestampMixin, table=True):
    __tablename__ = "knowledge_integrations"

    integration_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)

    created_by_user_id: UUID = Field(
        index=True, description="User who created/connected this integration"
    )
    created_by_admin: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True),
        description="Whether this integration was created by an admin user",
    )
    integration_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), index=True),
        description="Email/username of the connected account",
    )
    integration_scope: IntegrationScope = Field(
        default=IntegrationScope.ORGANIZATION,
        sa_column=Column(
            String(20),
            server_default=text("'organization'"),
            nullable=False,
            index=True,
        ),
        description="Access scope: ORGANIZATION, USER, TEAM, ROLE_BASED",
    )

    integration_type: IntegrationType = Field(sa_column=Column(String(50)))
    name: str = Field(description="User-defined name for the integration")

    # deal_id: UUID | None = Field(
    #     default=None,
    #     foreign_key="crm_deals.deal_id",
    #     index=True,
    #     description="Optional deal context for deal-specific integrations (e.g., Slack channel for a deal)",
    # )
    credentials: dict[str, Any] = Field(
        sa_column=Column(JSONB), description="Encrypted credentials for the integration"
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, server_default=text("true"), nullable=False, index=True),
    )
    last_synced_at: datetime | None = Field(default=None)

    connection_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), index=True),
        description="Unique connection identifier (e.g., from Nango)",
    )
    access_token_expires_at: datetime | None = Field(default=None)
    refresh_token_expires_at: datetime | None = Field(default=None)
    scopes: list[str] | None = Field(default=None, sa_column=Column(JSONB))
    connection_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        description="Provider-specific metadata",
    )

    sync_frequency: str = Field(
        default="daily",
        sa_column=Column(String(20), server_default=text("'daily'"), nullable=False),
    )
    last_sync_status: str | None = Field(default=None, sa_column=Column(String(20)))
    last_sync_error: str | None = Field(default=None)
    auto_sync_enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, server_default=text("true"), nullable=False),
    )

    sync_history: list["IntegrationSyncHistory"] = Relationship(back_populates="integration")


class SyncStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class IntegrationSyncHistory(TimestampMixin, table=True):
    __tablename__ = "integration_sync_history"
    __table_args__ = (
        Index("ix_sync_history_integration_started", "integration_id", "started_at"),
        Index("ix_sync_history_status", "status"),
    )

    sync_id: UUID = Field(default_factory=uuid4, primary_key=True)
    integration_id: UUID = Field(
        foreign_key="knowledge_integrations.integration_id",
        index=True,
        description="The integration that was synced.",
    )
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = Field(default=None)
    status: SyncStatus = Field(
        default=SyncStatus.QUEUED,
        sa_column=Column(String(20), server_default=text("'queued'"), nullable=False),
    )
    records_processed: int = Field(default=0)
    records_failed: int = Field(default=0)
    error_details: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    sync_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
    )

    integration: "KnowledgeIntegration" = Relationship(back_populates="sync_history")


__all__ = [
    "IntegrationType",
    "IntegrationScope",
    "KnowledgeIntegration",
    "SyncStatus",
    "IntegrationSyncHistory",
    "EMPTY_JSONB_SQL",
]
