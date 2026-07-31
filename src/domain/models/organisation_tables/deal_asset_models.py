from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field

from .base_mixins import TimestampMixin
from .tenant_constraints import tenant_foreign_key

EMPTY_JSONB_SQL = "'{}'::jsonb"


class DealAssetStatus(str, Enum):
    PENDING_UPLOAD = "pending_upload"
    PROCESSING = "processing"
    ACTIVE = "active"
    FAILED = "failed"


class DealAssetDirection(str, Enum):
    RECEIVED = "received"
    SHARED = "shared"
    INTERNAL = "internal"


class DealAssetSourceType(str, Enum):
    MANUAL_UPLOAD = "manual_upload"
    EMAIL_ATTACHMENT = "email_attachment"
    MEETING = "meeting"
    CRM = "crm"
    AI_EXTRACTED = "ai_extracted"


class DealAssetLinkStatus(str, Enum):
    OK = "ok"
    ACTIVE = "active"
    BROKEN = "broken"
    UNKNOWN = "unknown"


class DealAsset(TimestampMixin, table=True):
    """A file or link attached to a deal, tracked across its lifecycle."""

    __tablename__ = "deal_assets"
    __table_args__ = (
        tenant_foreign_key("deal_assets", "deal_id", "crm_deals", "deal_id"),
        tenant_foreign_key(
            "deal_assets",
            "kb_source_id",
            "organisation_sources",
            "source_id",
        ),
        Index("ix_deal_asset_org_deal", "organisation_id", "deal_id"),
        Index(
            "ix_deal_assets_org_kb_source_id",
            "organisation_id",
            "kb_source_id",
        ),
        Index("ix_deal_asset_deal_status", "deal_id", "status"),
        UniqueConstraint(
            "organisation_id",
            "deal_id",
            "source_type",
            "source_record_id",
            "filename",
            name="uq_deal_asset_source_dedup_org",
        ),
    )

    asset_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the deal asset.",
    )
    organisation_id: UUID = Field(index=True, description="The organisation this asset belongs to.")
    deal_id: UUID = Field(
        index=True,
        description="The deal this asset is associated with.",
    )
    added_by_user_id: UUID = Field(index=True, description="The user who added this asset.")

    name: str = Field(
        sa_column=Column(String(500), nullable=False),
        description="Display name of the asset.",
    )
    category: str | None = Field(
        default=None,
        sa_column=Column(String(100), nullable=True),
        description="Asset category (e.g. proposal, contract, deck).",
    )
    direction: str = Field(
        default=DealAssetDirection.INTERNAL.value,
        sa_column=Column(String(50), nullable=False, server_default=text("'internal'")),
        description="Direction of the asset: received, shared, or internal.",
    )
    status: str = Field(
        default=DealAssetStatus.ACTIVE.value,
        sa_column=Column(String(50), nullable=False, index=True, server_default=text("'active'")),
        description="Current status of the asset.",
    )

    # File storage fields
    storage_path: str | None = Field(
        default=None,
        sa_column=Column(String(1024), unique=True, nullable=True),
        description="Blob storage path for the uploaded file.",
    )
    filename: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Original filename.",
    )
    mimetype: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="MIME type of the file.",
    )
    size_bytes: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
        description="File size in bytes.",
    )
    checksum: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="File checksum (e.g. sha256).",
    )

    # Thumbnails
    thumbnail_small_path: str | None = Field(
        default=None,
        sa_column=Column(String(1024), nullable=True),
        description="Blob path for a small thumbnail.",
    )
    thumbnail_medium_path: str | None = Field(
        default=None,
        sa_column=Column(String(1024), nullable=True),
        description="Blob path for a medium thumbnail.",
    )

    # External link fields
    external_url: str | None = Field(
        default=None,
        sa_column=Column(String(2048), nullable=True),
        description="URL if the asset is an external link.",
    )
    external_source: str | None = Field(
        default=None,
        sa_column=Column(String(100), nullable=True),
        description="Source of the external link (e.g. google_drive, sharepoint).",
    )
    link_status: str | None = Field(
        default=None,
        sa_column=Column(String(50), nullable=True),
        description="Health status of the external link.",
    )
    link_checked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the link was last checked.",
    )

    # Knowledge-base linkage
    kb_source_id: UUID | None = Field(
        default=None,
        description="FK to knowledge base source if asset was indexed.",
    )

    # Provenance fields
    source_type: str = Field(
        default=DealAssetSourceType.MANUAL_UPLOAD.value,
        sa_column=Column(String(50), nullable=False, server_default=text("'manual_upload'")),
        description="How this asset was added.",
    )
    source_record_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="ID of the source record (e.g. email_id, meeting_id).",
    )
    source_context: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Human-readable context about the source.",
    )
    deal_stage_when_added: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Deal stage at the time the asset was added.",
    )

    # Metadata and soft-delete
    asset_metadata: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
        description="Arbitrary metadata (tags, AI-extracted fields, etc.).",
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Soft delete flag.",
    )
