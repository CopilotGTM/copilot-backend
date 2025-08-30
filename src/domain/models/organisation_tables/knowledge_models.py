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


class IntegrationType(str, Enum):
    # Collaboration & Communication
    SLACK = "slack"
    TEAMS = "teams"
    GOOGLE_CHAT = "google_chat"
    ZOOM = "zoom"
    GOOGLE_DRIVE = "google_drive"
    SHAREPOINT = "sharepoint"
    ONEDRIVE = "onedrive"
    BOX = "box"
    DROPBOX = "dropbox"
    NOTION = "notion"
    CONFLUENCE = "confluence"
    MIRO = "miro"
    FIGMA = "figma"

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
    name: str = Field(description="User-defined name for the integration (e.g., 'Engineering Slack')")
    credentials: Dict[str, Any] = Field(sa_column=Column(JSONB), description="Encrypted credentials for the integration")
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, server_default=text("true"), nullable=False, index=True),
        description="Whether the integration is currently active.",
    )
    last_synced_at: Optional[datetime] = Field(default=None)


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
        sa_column=Column(String(50), index=True, server_default=text("'file'"), nullable=False),
    )

    # --- Fields for FILE type ---
    filename: Optional[str] = Field(default=None, description="The original filename of the source document.")
    storage_path: Optional[str] = Field(
        default=None, unique=True, description="The path to the file in the storage backend."
    )
    # Two thumbnail variants: small and medium
    thumbnail_small_path: Optional[str] = Field(
        default=None, description="Blob path for a small thumbnail (e.g., 128px)"
    )
    thumbnail_medium_path: Optional[str] = Field(
        default=None, description="Blob path for a medium thumbnail (e.g., 512px)"
    )
    mimetype: Optional[str] = Field(default=None, description="The MIME type of the file.")
    size_bytes: Optional[int] = Field(default=None, description="The size of the file in bytes.")
    # Checksum (single field for optional client or server checksum)
    checksum: Optional[str] = Field(default=None, index=True, description="Optional checksum for the file (e.g. sha256)")
    
    # --- Fields for INTEGRATION type ---
    integration_id: Optional[UUID] = Field(
        default=None,
        foreign_key="knowledge_integrations.integration_id",
        index=True,
    )
    last_synced_at: Optional[datetime] = Field(default=None)

    status: OrganisationSourceStatus = Field(
        default=OrganisationSourceStatus.PENDING_UPLOAD,
        sa_column=Column(String(50), index=True, server_default=text("'pending_upload'"), nullable=False),
        description="The current processing status of the document.",
    )
    error_message: Optional[str] = Field(
        default=None, description="Stores any error message if processing failed."
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True),
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
        sa_column=Column(Boolean, server_default=text("true"), nullable=False, index=True),
        description="True => visible to all users in organisation; False => personal to uploader_user_id.",
    )
    deal_id: Optional[str] = Field(
        default=None,
        index=True,
        description="Optional deal identifier for grouping (can be null).",
    )
    name: Optional[str] = Field(default=None, index=True)
    meta_data: Dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
    )
    filters: Dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
    )
    content: Optional[str] = Field(default=None)
    embedding: Optional[List[float]] = Field(
        sa_column=Column(Vector),
        default=None,
    )
    usage: Optional[Dict[str, Any]] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
    )
    content_hash: Optional[str] = Field(default=None, index=True)

    is_searchable: bool = Field(
        default=True,
        sa_column=Column(Boolean, server_default=text("true"), nullable=False, index=True),
        description="If False exclude from search results (draft or archived).",
    )
    is_archived: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True),
        description="Soft archive flag; archived docs hidden from normal queries.",
    )
    source: Optional["OrganisationSource"] = Relationship(back_populates="documents")