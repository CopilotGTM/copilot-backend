from enum import Enum
from typing import Any, Dict, Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4
from datetime import datetime

from sqlmodel import SQLModel, Field, Column, Relationship
from sqlalchemy import text, String, CheckConstraint, Index, UniqueConstraint, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .job_models import KnowledgeProcessingJob


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


class KnowledgeIntegration(TimestampMixin, table=True):
    __tablename__ = "knowledge_integrations"

    integration_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    integration_type: IntegrationType = Field(sa_column=Column(String(50)))
    name: str = Field(
        description="User-defined name for the integration (e.g., 'Engineering Slack')"
    )
    credentials: Dict[str, Any] = Field(
        sa_column=Column(JSONB), description="Encrypted credentials for the integration"
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(
            Boolean, server_default=text("true"), nullable=False, index=True
        ),
        description="Whether the integration is currently active.",
    )
    last_synced_at: Optional[datetime] = Field(default=None)

    # OAuth and token management fields
    connection_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String(255), index=True),
        description="Unique connection identifier for this integration (e.g., from Nango or other OAuth providers)",
    )
    access_token_expires_at: Optional[datetime] = Field(
        default=None,
        description="When the current access token expires",
    )
    refresh_token_expires_at: Optional[datetime] = Field(
        default=None,
        description="When the refresh token expires (if applicable)",
    )
    scopes: Optional[List[str]] = Field(
        default=None,
        sa_column=Column(JSONB),
        description="OAuth scopes granted for this integration",
    )
    connection_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional metadata from Nango connection (e.g., account info, user details)",
    )

    # Sync configuration and status
    sync_frequency: str = Field(
        default="daily",
        sa_column=Column(String(20), server_default=text("'daily'"), nullable=False),
        description="How often to sync data: 'hourly', 'daily', 'weekly', or 'manual'",
    )
    last_sync_status: Optional[str] = Field(
        default=None,
        sa_column=Column(String(20)),
        description="Status of last sync: 'success', 'failed', 'partial', 'in_progress'",
    )
    last_sync_error: Optional[str] = Field(
        default=None,
        description="Error message from last failed sync attempt",
    )
    auto_sync_enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, server_default=text("true"), nullable=False),
        description="Whether automatic syncing is enabled for this integration",
    )

    # Relationships
    sync_history: List["IntegrationSyncHistory"] = Relationship(
        back_populates="integration"
    )


class SourceType(str, Enum):
    FILE = "file"
    INTEGRATION = "integration"


class OrganisationSourceStatus(str, Enum):
    """Lifecycle states for a knowledge source.

    PENDING_UPLOAD   -> Record created; client has not (necessarily) uploaded blob yet.
    UPLOADED         -> Blob is confirmed present (basic properties fetched) and ready for processing.
    PROCESSING       -> Background tasks are performing checksum / extraction / chunking.
    CHUNKING         -> (Reserved) Specifically performing chunk & embed (optional future granularity).
    COMPLETED        -> All processing succeeded; source ready for retrieval / search.
    FAILED           -> Terminal error state.
    """

    PENDING_UPLOAD = "pending_upload"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    CHUNKING = "chunking"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    FAILED = "failed"


class OrganisationSource(TimestampMixin, table=True):
    """
    Represents an original, uploaded document that serves as a source
    for the knowledge base. This is the master record for an uploaded file.
    """

    __tablename__ = "organisation_sources"

    source_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the knowledge source.",
    )
    organisation_id: UUID = Field(
        index=True, description="The organisation this source belongs to."
    )
    user_id: UUID = Field(index=True, description="The user who uploaded the source.")

    source_type: SourceType = Field(
        default=SourceType.FILE,
        sa_column=Column(
            String(50), index=True, server_default=text("'file'"), nullable=False
        ),
    )

    # --- Fields for FILE type ---
    filename: Optional[str] = Field(
        default=None, description="The original filename of the source document."
    )
    storage_path: Optional[str] = Field(
        default=None,
        unique=True,
        description="The path to the file in the storage backend.",
    )
    # Two thumbnail variants: small and medium
    thumbnail_small_path: Optional[str] = Field(
        default=None, description="Blob path for a small thumbnail (e.g., 128px)"
    )
    thumbnail_medium_path: Optional[str] = Field(
        default=None, description="Blob path for a medium thumbnail (e.g., 512px)"
    )
    mimetype: Optional[str] = Field(
        default=None, description="The MIME type of the file."
    )
    size_bytes: Optional[int] = Field(
        default=None, description="The size of the file in bytes."
    )
    # Checksum (single field for optional client or server checksum)
    checksum: Optional[str] = Field(
        default=None,
        index=True,
        description="Optional checksum for the file (e.g. sha256)",
    )

    # --- Fields for INTEGRATION type ---
    integration_id: Optional[UUID] = Field(
        default=None,
        foreign_key="knowledge_integrations.integration_id",
        index=True,
    )
    last_synced_at: Optional[datetime] = Field(default=None)

    status: OrganisationSourceStatus = Field(
        default=OrganisationSourceStatus.PENDING_UPLOAD,
        sa_column=Column(
            String(50),
            index=True,
            server_default=text("'pending_upload'"),
            nullable=False,
        ),
        description="The current processing status of the document.",
    )
    error_message: Optional[str] = Field(
        default=None, description="Stores any error message if processing failed."
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(
            Boolean, server_default=text("false"), nullable=False, index=True
        ),
        description="Soft delete flag; deleted sources hidden from normal queries.",
    )

    # This links a source document to all the chunks that were derived from it.
    documents: List["OrganisationDocument"] = Relationship(back_populates="source")
    integration: Optional["KnowledgeIntegration"] = Relationship()
    jobs: List["KnowledgeProcessingJob"] = Relationship(back_populates="source")


class OrganisationDocument(TimestampMixin, table=True):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        CheckConstraint(
            "(is_org_wide OR uploader_user_id IS NOT NULL)",
            name="ck_kb_owner_required",
        ),
        Index(
            "ix_kb_org_visibility",
            "is_org_wide",
            "is_archived",
        ),
        Index(
            "ix_kb_org_deal",
            "deal_id",
            postgresql_where=text("deal_id IS NOT NULL"),
        ),
        UniqueConstraint(
            "content_hash",
            name="uq_kb_org_content_hash",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    document_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the knowledge base document/chunk.",
    )
    source_id: Optional[UUID] = Field(
        default=None,
        foreign_key="organisation_sources.source_id",
        index=True,
        description="Foreign key to the original source document.",
    )
    uploader_user_id: Optional[UUID] = Field(
        default=None,
        index=True,
        description="User who uploaded the document (admin or end-user).",
    )
    is_org_wide: bool = Field(
        default=True,
        sa_column=Column(
            Boolean, server_default=text("true"), nullable=False, index=True
        ),
        description="True => visible to all users in organisation; False => personal to uploader_user_id.",
    )
    deal_id: Optional[str] = Field(
        default=None,
        index=True,
        description="Optional deal identifier for grouping (can be null).",
    )
    name: Optional[str] = Field(default=None, index=True)
    meta_data: Dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
    )
    filters: Dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
    )
    content: Optional[str] = Field(default=None)
    embedding: Optional[List[float]] = Field(
        sa_column=Column(Vector),
        default=None,
    )
    usage: Optional[Dict[str, Any]] = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
    )
    content_hash: Optional[str] = Field(default=None, index=True)

    is_searchable: bool = Field(
        default=True,
        sa_column=Column(
            Boolean, server_default=text("true"), nullable=False, index=True
        ),
        description="If False exclude from search results (draft or archived).",
    )
    is_archived: bool = Field(
        default=False,
        sa_column=Column(
            Boolean, server_default=text("false"), nullable=False, index=True
        ),
        description="Soft archive flag; archived docs hidden from normal queries.",
    )
    source: Optional["OrganisationSource"] = Relationship(back_populates="documents")


class SyncStatus(str, Enum):
    """Status values for integration sync operations."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class IntegrationSyncHistory(TimestampMixin, table=True):
    """
    Tracks the history of all sync operations for integrations.
    Used for auditing, debugging, and monitoring sync performance.
    """

    __tablename__ = "integration_sync_history"
    __table_args__ = (
        Index(
            "ix_sync_history_integration_started",
            "integration_id",
            "started_at",
        ),
        Index(
            "ix_sync_history_status",
            "status",
        ),
    )

    sync_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the sync operation.",
    )
    integration_id: UUID = Field(
        foreign_key="knowledge_integrations.integration_id",
        index=True,
        description="The integration that was synced.",
    )
    started_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When the sync operation started.",
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="When the sync operation completed (success or failure).",
    )
    status: SyncStatus = Field(
        default=SyncStatus.QUEUED,
        sa_column=Column(String(20), server_default=text("'queued'"), nullable=False),
        description="Current status of the sync operation.",
    )
    records_processed: int = Field(
        default=0,
        description="Number of records successfully processed.",
    )
    records_failed: int = Field(
        default=0,
        description="Number of records that failed to process.",
    )
    error_details: Optional[Dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Detailed error information if sync failed.",
    )
    sync_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional metadata about the sync (e.g., types of records synced, API calls made).",
    )

    # Relationships
    integration: "KnowledgeIntegration" = Relationship(back_populates="sync_history")
