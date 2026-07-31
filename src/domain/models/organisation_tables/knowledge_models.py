from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, Relationship

from .base_mixins import TimestampMixin
from .sync.integration_models import (
    EMPTY_JSONB_SQL,
    KnowledgeIntegration,
)
from .tenant_constraints import tenant_foreign_key

if TYPE_CHECKING:
    from .job_models import KnowledgeProcessingJob


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
    DELETING         -> Deletion in progress (background cleanup running).
    """

    PENDING_UPLOAD = "pending_upload"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    CHUNKING = "chunking"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETING = "deleting"


class OrganisationSource(TimestampMixin, table=True):
    """
    Represents an original, uploaded document that serves as a source
    for the knowledge base. This is the master record for an uploaded file.
    """

    __tablename__ = "organisation_sources"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "source_id",
            name="uq_organisation_sources_org_source_id",
        ),
        tenant_foreign_key(
            "organisation_sources",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
    )

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
    filename: str | None = Field(
        default=None, description="The original filename of the source document."
    )
    storage_path: str | None = Field(
        default=None,
        unique=True,
        description="The path to the file in the storage backend.",
    )
    # Two thumbnail variants: small and medium
    thumbnail_small_path: str | None = Field(
        default=None, description="Blob path for a small thumbnail (e.g., 128px)"
    )
    thumbnail_medium_path: str | None = Field(
        default=None, description="Blob path for a medium thumbnail (e.g., 512px)"
    )
    mimetype: str | None = Field(default=None, description="The MIME type of the file.")
    size_bytes: int | None = Field(default=None, description="The size of the file in bytes.")
    # Checksum (single field for optional client or server checksum)
    checksum: str | None = Field(
        default=None,
        index=True,
        description="Optional checksum for the file (e.g. sha256)",
    )

    # --- Fields for INTEGRATION type ---
    integration_id: UUID | None = Field(
        default=None,
        index=True,
    )
    last_synced_at: datetime | None = Field(default=None)

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
    error_message: str | None = Field(
        default=None, description="Stores any error message if processing failed."
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False, index=True),
        description="Soft delete flag; deleted sources hidden from normal queries.",
    )

    # This links a source document to all the chunks that were derived from it.
    documents: list["OrganisationDocument"] = Relationship(back_populates="source")
    integration: Optional["KnowledgeIntegration"] = Relationship()
    jobs: list["KnowledgeProcessingJob"] = Relationship(back_populates="source")


class OrganisationDocument(TimestampMixin, table=True):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        tenant_foreign_key(
            "knowledge_base",
            "source_id",
            "organisation_sources",
            "source_id",
        ),
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
            "organisation_id",
            "content_hash",
            name="uq_kb_org_content_hash_scoped",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    document_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the knowledge base document/chunk.",
    )
    organisation_id: UUID = Field(
        foreign_key="public.organisations.organisation_id",
        nullable=False,
        index=True,
        sa_column_kwargs={
            "server_default": text(
                "NULLIF(current_setting('app.current_organisation_id', true), '')::uuid"
            )
        },
    )
    source_id: UUID | None = Field(
        default=None,
        index=True,
        description="Foreign key to the original source document.",
    )
    uploader_user_id: UUID | None = Field(
        default=None,
        index=True,
        description="User who uploaded the document (admin or end-user).",
    )
    is_org_wide: bool = Field(
        default=True,
        sa_column=Column(Boolean, server_default=text("true"), nullable=False, index=True),
        description="True => visible to all users in organisation; False => personal to uploader_user_id.",
    )
    deal_id: str | None = Field(
        default=None,
        index=True,
        description="Optional deal identifier for grouping (can be null).",
    )
    name: str | None = Field(default=None, index=True)
    meta_data: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
    )
    filters: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
    )
    content: str | None = Field(default=None)
    embedding: list[float] | None = Field(
        sa_column=Column(Vector),
        default=None,
    )
    usage: dict[str, Any] | None = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
    )
    content_hash: str | None = Field(default=None, index=True)

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
