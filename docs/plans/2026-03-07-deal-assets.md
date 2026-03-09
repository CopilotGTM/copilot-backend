# Deal Assets Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `deal_assets` table and full CRUD API so users can manage uploaded files and external links associated with a deal — the "Deal Assets" tab in the deal detail view.

**Architecture:** Single new `deal_assets` table in org schemas, a `DealAssetsService` for business logic, a presign/finalize upload flow (matching existing chat attachment pattern), and a REST API under `/users/crm/deals/{deal_id}/assets`. Files stored in Azure Blob under `deal_assets/{user_id}/{asset_id}/{filename}`.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, Alembic (org migrations), PostgreSQL + JSONB, Azure Blob Storage (SAS URLs), Pydantic v2 schemas.

---

## Task 1: Define the DealAsset Model and Enums

**Files:**

- Create: `src/domain/models/organisation_tables/deal_asset_models.py`
- Modify: `src/domain/models/organisation_tables/__init__.py`

**Step 1: Create the model file**

```python
"""Deal asset models — uploaded files and external links tied to a deal."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from .base_mixins import TimestampMixin

# ── Enums ────────────────────────────────────────────────────────────────

EMPTY_JSONB_SQL = "'{}'::jsonb"


class DealAssetStatus(str, Enum):
    """Lifecycle status for a deal asset."""

    PENDING_UPLOAD = "pending_upload"
    ACTIVE = "active"
    FAILED = "failed"


class DealAssetDirection(str, Enum):
    """Who originated or owns the asset."""

    RECEIVED = "received"
    SHARED = "shared"
    INTERNAL = "internal"


class DealAssetSourceType(str, Enum):
    """How the asset entered the system."""

    MANUAL_UPLOAD = "manual_upload"
    EMAIL_ATTACHMENT = "email_attachment"
    MEETING = "meeting"
    CRM = "crm"
    AI_EXTRACTED = "ai_extracted"


class DealAssetLinkStatus(str, Enum):
    """Health of an external link."""

    OK = "ok"
    ACTIVE = "active"
    BROKEN = "broken"
    UNKNOWN = "unknown"


# ── Model ────────────────────────────────────────────────────────────────


class DealAsset(TimestampMixin, table=True):
    """A file or external link associated with a deal.

    When storage_path is set the asset is a stored file (Azure Blob).
    When external_url is set the asset is a link to an outside resource.
    Both can be set simultaneously (downloaded copy + original provider link).
    """

    __tablename__ = "deal_assets"

    __table_args__ = (
        Index("ix_deal_asset_org_deal", "organisation_id", "deal_id"),
        Index("ix_deal_asset_deal_status", "deal_id", "status"),
        UniqueConstraint(
            "deal_id",
            "source_type",
            "source_record_id",
            "filename",
            name="uq_deal_asset_source_dedup",
        ),
    )

    # ── Primary key ──────────────────────────────────────────────────
    asset_id: UUID = Field(default_factory=uuid4, primary_key=True)

    # ── Ownership ────────────────────────────────────────────────────
    organisation_id: UUID = Field(index=True, description="Tenant isolation.")
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="The deal this asset belongs to.",
    )
    added_by_user_id: UUID = Field(
        index=True,
        description="User who added the asset.",
    )

    # ── Display ──────────────────────────────────────────────────────
    name: str = Field(
        max_length=500,
        description="Display name shown in the UI (filename or user-provided label).",
    )
    category: str | None = Field(
        default=None,
        max_length=100,
        sa_column=Column(String(100), nullable=True),
        description="Asset category: rfp, proposal, contract, security, demo, presentation, meeting_recording, internal, other.",
    )
    direction: str = Field(
        default=DealAssetDirection.INTERNAL.value,
        sa_column=Column(
            String(50),
            nullable=False,
            server_default=text("'internal'"),
        ),
        description="received | shared | internal",
    )

    # ── Lifecycle ────────────────────────────────────────────────────
    status: str = Field(
        default=DealAssetStatus.ACTIVE.value,
        sa_column=Column(
            String(50),
            nullable=False,
            server_default=text("'active'"),
            index=True,
        ),
        description="pending_upload | active | failed",
    )

    # ── File storage (nullable — set when blob exists) ───────────────
    storage_path: str | None = Field(
        default=None,
        sa_column=Column(String(1024), unique=True, nullable=True),
        description="Azure Blob path within the org container.",
    )
    filename: str | None = Field(
        default=None,
        max_length=255,
        description="Original filename.",
    )
    mimetype: str | None = Field(
        default=None,
        max_length=255,
        description="MIME type of the stored file.",
    )
    size_bytes: int | None = Field(
        default=None,
        description="File size in bytes.",
    )
    checksum: str | None = Field(
        default=None,
        max_length=128,
        description="Optional integrity hash (MD5/SHA-256).",
    )
    thumbnail_small_path: str | None = Field(
        default=None,
        max_length=1024,
        description="Blob path for small thumbnail (128px).",
    )
    thumbnail_medium_path: str | None = Field(
        default=None,
        max_length=1024,
        description="Blob path for medium thumbnail (512px).",
    )

    # ── External link (nullable — set when link exists) ──────────────
    external_url: str | None = Field(
        default=None,
        max_length=2048,
        description="URL to external resource (Drive, SharePoint, Confluence, etc.).",
    )
    external_source: str | None = Field(
        default=None,
        max_length=100,
        sa_column=Column(String(100), nullable=True),
        description="Provider key: onedrive, google_drive, sharepoint, confluence, gong, etc.",
    )
    link_status: str | None = Field(
        default=None,
        sa_column=Column(String(50), nullable=True),
        description="ok | active | broken | unknown",
    )
    link_checked_at: datetime | None = Field(
        default=None,
        description="Last time the link was health-checked.",
    )

    # ── KB linkage (nullable — set when also in knowledge base) ──────
    kb_source_id: UUID | None = Field(
        default=None,
        foreign_key="organisation_sources.source_id",
        description="Links to KB source if asset is also indexed for search.",
    )

    # ── Provenance ───────────────────────────────────────────────────
    source_type: str = Field(
        default=DealAssetSourceType.MANUAL_UPLOAD.value,
        sa_column=Column(
            String(50),
            nullable=False,
            server_default=text("'manual_upload'"),
        ),
        description="How this asset entered: manual_upload | email_attachment | meeting | crm | ai_extracted",
    )
    source_record_id: str | None = Field(
        default=None,
        max_length=512,
        description="ID of the originating record (email_id, meeting_id, etc.).",
    )
    source_context: str | None = Field(
        default=None,
        max_length=500,
        description="Human-readable source context, e.g. 'Email: RE: Proposal Review — Mar 5'.",
    )

    # ── Stage snapshot ───────────────────────────────────────────────
    deal_stage_when_added: str | None = Field(
        default=None,
        max_length=255,
        description="Snapshot of the deal stage name when this asset was added.",
    )

    # ── Extensible metadata ──────────────────────────────────────────
    asset_metadata: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text(EMPTY_JSONB_SQL)),
        default_factory=dict,
        description="Extensible metadata bag.",
    )

    # ── Soft delete ──────────────────────────────────────────────────
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(
            "is_deleted",
            String(5).__class__.__bases__[0].__subclasses__()[0]
            if False
            else __import__("sqlalchemy").Boolean,
            nullable=False,
            server_default=text("false"),
        ),
        description="Soft-delete flag.",
    )
```

**Wait — the `is_deleted` definition above is hacky.** Use the clean pattern from existing models:

Replace the `is_deleted` field with:

```python
    from sqlalchemy import Boolean

    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
```

Make sure `Boolean` is imported from `sqlalchemy` at the top of the file alongside `Column`, `Index`, `String`, etc.

**Step 2: Register the model in `__init__.py`**

Add to `src/domain/models/organisation_tables/__init__.py`:

```python
# In imports section:
from .deal_asset_models import (
    DealAsset,
    DealAssetDirection,
    DealAssetLinkStatus,
    DealAssetSourceType,
    DealAssetStatus,
)

# In __all__ list — add alongside other deal-related exports:
    "DealAsset",
    "DealAssetDirection",
    "DealAssetLinkStatus",
    "DealAssetSourceType",
    "DealAssetStatus",
```

**Step 3: Verify model imports work**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run python -c "from src.domain.models.organisation_tables import DealAsset; print('OK:', DealAsset.__tablename__)"`

Expected: `OK: deal_assets`

**Step 4: Commit**

```bash
git add src/domain/models/organisation_tables/deal_asset_models.py src/domain/models/organisation_tables/__init__.py
git commit -m "feat(deal-assets): add DealAsset model and enums"
```

---

## Task 2: Create the Alembic Migration

**Files:**

- Create: `migrations/org_migrations/versions/3c7f79637798_030_add_deal_assets.py`

**Step 1: Create the migration file**

The latest migration is `3c7f79637797` (029). The new one is `3c7f79637798` (030).

```python
"""Add deal_assets table.

Revision ID: 3c7f79637798
Revises: 3c7f79637797
Create Date: 2026-03-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637798"
down_revision: str | None = "3c7f79637797"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_assets",
        # PK
        sa.Column(
            "asset_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Ownership
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("added_by_user_id", sa.UUID(), nullable=False),
        # Display
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column(
            "direction",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        # Lifecycle
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        # File storage
        sa.Column("storage_path", sa.String(1024), nullable=True, unique=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("mimetype", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum", sa.String(128), nullable=True),
        sa.Column("thumbnail_small_path", sa.String(1024), nullable=True),
        sa.Column("thumbnail_medium_path", sa.String(1024), nullable=True),
        # External link
        sa.Column("external_url", sa.String(2048), nullable=True),
        sa.Column("external_source", sa.String(100), nullable=True),
        sa.Column("link_status", sa.String(50), nullable=True),
        sa.Column("link_checked_at", sa.DateTime(timezone=True), nullable=True),
        # KB linkage
        sa.Column("kb_source_id", sa.UUID(), nullable=True),
        # Provenance
        sa.Column(
            "source_type",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'manual_upload'"),
        ),
        sa.Column("source_record_id", sa.String(512), nullable=True),
        sa.Column("source_context", sa.String(500), nullable=True),
        # Stage snapshot
        sa.Column("deal_stage_when_added", sa.String(255), nullable=True),
        # Metadata
        sa.Column(
            "asset_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Soft delete
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Constraints
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"], name="fk_deal_asset_deal"),
        sa.ForeignKeyConstraint(
            ["kb_source_id"],
            ["organisation_sources.source_id"],
            name="fk_deal_asset_kb_source",
        ),
        sa.UniqueConstraint(
            "deal_id",
            "source_type",
            "source_record_id",
            "filename",
            name="uq_deal_asset_source_dedup",
        ),
    )

    # Indexes
    op.create_index("ix_deal_asset_org_id", "deal_assets", ["organisation_id"])
    op.create_index("ix_deal_asset_deal_id", "deal_assets", ["deal_id"])
    op.create_index("ix_deal_asset_added_by", "deal_assets", ["added_by_user_id"])
    op.create_index("ix_deal_asset_org_deal", "deal_assets", ["organisation_id", "deal_id"])
    op.create_index("ix_deal_asset_deal_status", "deal_assets", ["deal_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_deal_asset_deal_status", table_name="deal_assets")
    op.drop_index("ix_deal_asset_org_deal", table_name="deal_assets")
    op.drop_index("ix_deal_asset_added_by", table_name="deal_assets")
    op.drop_index("ix_deal_asset_deal_id", table_name="deal_assets")
    op.drop_index("ix_deal_asset_org_id", table_name="deal_assets")
    op.drop_table("deal_assets")
```

**Step 2: Verify migration syntax**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run python -c "import importlib.util; spec = importlib.util.spec_from_file_location('m', 'migrations/org_migrations/versions/3c7f79637798_030_add_deal_assets.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); print('OK: revision', mod.revision)"`

Expected: `OK: revision 3c7f79637798`

**Step 3: Commit**

```bash
git add migrations/org_migrations/versions/3c7f79637798_030_add_deal_assets.py
git commit -m "feat(deal-assets): add deal_assets migration (030)"
```

---

## Task 3: Create Pydantic Request/Response Schemas

**Files:**

- Create: `src/api/schemas/deal_asset_models.py`

**Step 1: Create the schema file**

```python
"""Pydantic schemas for the Deal Assets API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator


# ── Request schemas ──────────────────────────────────────────────────────


class DealAssetPresignRequest(BaseModel):
    """Request a presigned upload URL for a deal asset file."""

    filename: str = Field(..., min_length=1, max_length=255)
    mimetype: str = Field(..., max_length=255)
    size_bytes: int = Field(..., gt=0)
    name: str | None = Field(
        default=None,
        max_length=500,
        description="Display name. Defaults to filename if omitted.",
    )
    category: str | None = Field(default=None, max_length=100)
    direction: str = Field(default="internal", pattern="^(received|shared|internal)$")

    @field_validator("filename")
    @classmethod
    def sanitize_filename(cls, v: str) -> str:
        return v.strip()


class DealAssetFinalizeRequest(BaseModel):
    """Confirm that a file has been uploaded to the presigned URL."""

    asset_id: UUID
    checksum: str | None = Field(default=None, max_length=128)


class DealAssetLinkCreateRequest(BaseModel):
    """Add an external link as a deal asset."""

    name: str = Field(..., min_length=1, max_length=500)
    external_url: str = Field(..., min_length=1, max_length=2048)
    external_source: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    direction: str = Field(default="internal", pattern="^(received|shared|internal)$")

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("Name cannot be empty.")
        return value

    @field_validator("external_url")
    @classmethod
    def url_not_empty(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("URL cannot be empty.")
        return value


class DealAssetUpdateRequest(BaseModel):
    """Partial update for an existing deal asset."""

    name: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=100)
    direction: str | None = Field(default=None, pattern="^(received|shared|internal)$")
    external_url: str | None = Field(default=None, max_length=2048)
    external_source: str | None = Field(default=None, max_length=100)
    link_status: str | None = Field(default=None, pattern="^(ok|active|broken|unknown)$")


# ── Response schemas ─────────────────────────────────────────────────────


class DealAssetItem(BaseModel):
    """Single deal asset in API responses."""

    asset_id: UUID
    deal_id: UUID
    name: str
    category: str | None = None
    direction: str
    status: str

    # File fields
    filename: str | None = None
    mimetype: str | None = None
    size_bytes: int | None = None
    download_url: str | None = None
    thumbnail_small_url: str | None = None
    thumbnail_medium_url: str | None = None

    # Link fields
    external_url: str | None = None
    external_source: str | None = None
    link_status: str | None = None
    link_checked_at: datetime | None = None

    # KB linkage
    kb_source_id: UUID | None = None

    # Provenance
    source_type: str
    source_context: str | None = None
    deal_stage_when_added: str | None = None

    # Metadata
    added_by_user_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DealAssetPresignResponse(BaseModel):
    """Response after requesting a presigned upload URL."""

    asset_id: UUID
    filename: str
    upload_url: HttpUrl
    expires_in_seconds: int
    status: str


class DealAssetsPage(BaseModel):
    """Paginated list of deal assets."""

    items: list[DealAssetItem]
    total_count: int


# ── Batch variants (for future email attachment ingestion) ───────────────


class DealAssetBatchPresignRequest(BaseModel):
    """Batch presign for multiple file assets at once."""

    files: list[DealAssetPresignRequest] = Field(..., min_length=1)


class DealAssetBatchPresignResponse(BaseModel):
    """Batch presign results."""

    files: list[DealAssetPresignResponse]
    errors: list[dict] = Field(default_factory=list)
```

**Step 2: Verify schema imports**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run python -c "from src.api.schemas.deal_asset_models import DealAssetItem, DealAssetPresignRequest; print('OK')"`

Expected: `OK`

**Step 3: Commit**

```bash
git add src/api/schemas/deal_asset_models.py
git commit -m "feat(deal-assets): add Pydantic request/response schemas"
```

---

## Task 4: Create the DealAssetsService

**Files:**

- Create: `src/service/crm/deal_assets_service.py`

**Step 1: Create the service file**

```python
"""Service for managing deal assets (files and external links)."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger

logger = get_logger("deal_assets_service")

_WRITE_SAS_MINUTES = 15
_READ_SAS_MINUTES = 120


class DealAssetsServiceError(Exception):
    """Custom exception with HTTP status code."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealAssetsService:
    """CRUD operations for deal assets scoped to an organisation."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def close(self) -> None:
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("DealAssetsService close exception", exc_info=True)

    # ── Queries ──────────────────────────────────────────────────────

    async def list_assets(
        self,
        deal_id: UUID,
    ) -> tuple[list[DealAsset], int]:
        """Return all active assets for a deal, newest first."""
        c = cast(Any, DealAsset).__table__.c
        base = select(DealAsset).where(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            c.is_deleted.is_(False),
            c.status != DealAssetStatus.FAILED.value,
        )

        # Total count
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.org_session.execute(count_stmt)).scalar() or 0

        # Fetch all (deal assets are a bounded set — no pagination needed yet)
        stmt = base.order_by(c.created_at.desc())
        result = await self.org_session.execute(stmt)
        return list(result.scalars().all()), total

    async def get_asset(self, deal_id: UUID, asset_id: UUID) -> DealAsset:
        """Fetch a single asset, enforcing deal + org ownership."""
        c = cast(Any, DealAsset).__table__.c
        stmt = select(DealAsset).where(
            c.asset_id == asset_id,
            c.deal_id == deal_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        result = await self.org_session.execute(stmt)
        asset = result.scalar_one_or_none()
        if not asset:
            raise DealAssetsServiceError("Asset not found.", status_code=404)
        return asset

    # ── File upload (presign / finalize) ─────────────────────────────

    async def presign_file_upload(
        self,
        deal_id: UUID,
        user_id: UUID,
        filename: str,
        mimetype: str,
        size_bytes: int,
        name: str | None = None,
        category: str | None = None,
        direction: str = "internal",
    ) -> tuple[DealAsset, str, int]:
        """Create a pending asset record and return a write SAS URL.

        Returns:
            (asset, upload_url, expires_in_seconds)
        """
        asset_id = uuid4()
        storage_path = f"deal_assets/{user_id}/{asset_id}/{filename}"

        asset = DealAsset(
            asset_id=asset_id,
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            added_by_user_id=user_id,
            name=name or filename,
            category=category,
            direction=direction,
            status=DealAssetStatus.PENDING_UPLOAD.value,
            storage_path=storage_path,
            filename=filename,
            mimetype=mimetype,
            size_bytes=size_bytes,
            source_type="manual_upload",
        )
        self.org_session.add(asset)
        await self.org_session.flush()

        # Generate write SAS outside of long-held session in handler
        blob = AzureOrganisationBlobStorage(self.organisation_id)
        upload_url = await blob.generate_write_sas(
            blob_path=storage_path,
            content_type=mimetype,
            minutes=_WRITE_SAS_MINUTES,
        )
        return asset, upload_url, _WRITE_SAS_MINUTES * 60

    async def finalize_file_upload(
        self,
        deal_id: UUID,
        asset_id: UUID,
        checksum: str | None = None,
    ) -> DealAsset:
        """Transition asset from pending_upload → active after client uploads."""
        asset = await self.get_asset(deal_id, asset_id)
        if asset.status != DealAssetStatus.PENDING_UPLOAD.value:
            raise DealAssetsServiceError(
                f"Asset is in '{asset.status}' state, not pending_upload.",
                status_code=409,
            )
        asset.status = DealAssetStatus.ACTIVE.value
        if checksum:
            asset.checksum = checksum
        self.org_session.add(asset)
        await self.org_session.flush()
        return asset

    # ── External link ────────────────────────────────────────────────

    async def create_link(
        self,
        deal_id: UUID,
        user_id: UUID,
        name: str,
        external_url: str,
        external_source: str | None = None,
        category: str | None = None,
        direction: str = "internal",
    ) -> DealAsset:
        """Create an external-link deal asset (no file upload needed)."""
        asset = DealAsset(
            asset_id=uuid4(),
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            added_by_user_id=user_id,
            name=name,
            category=category,
            direction=direction,
            status=DealAssetStatus.ACTIVE.value,
            external_url=external_url,
            external_source=external_source,
            link_status="unknown",
            source_type="manual_upload",
        )
        self.org_session.add(asset)
        await self.org_session.flush()
        return asset

    # ── Update / Delete ──────────────────────────────────────────────

    async def update_asset(
        self,
        deal_id: UUID,
        asset_id: UUID,
        updates: dict[str, Any],
    ) -> DealAsset:
        """Apply partial updates to a deal asset."""
        asset = await self.get_asset(deal_id, asset_id)
        for field, value in updates.items():
            if hasattr(asset, field):
                setattr(asset, field, value)
        self.org_session.add(asset)
        await self.org_session.flush()
        return asset

    async def delete_asset(self, deal_id: UUID, asset_id: UUID) -> DealAsset:
        """Soft-delete a deal asset."""
        asset = await self.get_asset(deal_id, asset_id)
        asset.is_deleted = True
        self.org_session.add(asset)
        await self.org_session.flush()
        return asset

    # ── SAS URL helpers ──────────────────────────────────────────────

    async def generate_download_url(self, asset: DealAsset) -> str | None:
        """Generate a read SAS URL for a stored file asset."""
        if not asset.storage_path:
            return None
        blob = AzureOrganisationBlobStorage(self.organisation_id)
        return await blob.generate_read_sas(
            blob_path=asset.storage_path,
            minutes=_READ_SAS_MINUTES,
            download_filename=asset.filename,
        )

    async def generate_thumbnail_urls(
        self, asset: DealAsset
    ) -> tuple[str | None, str | None]:
        """Generate read SAS URLs for thumbnails."""
        blob = AzureOrganisationBlobStorage(self.organisation_id)
        small = None
        medium = None
        if asset.thumbnail_small_path:
            small = await blob.generate_read_sas(asset.thumbnail_small_path, minutes=_READ_SAS_MINUTES)
        if asset.thumbnail_medium_path:
            medium = await blob.generate_read_sas(asset.thumbnail_medium_path, minutes=_READ_SAS_MINUTES)
        return small, medium
```

**Step 2: Verify service imports**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run python -c "from src.service.crm.deal_assets_service import DealAssetsService; print('OK')"`

Expected: `OK`

**Step 3: Commit**

```bash
git add src/service/crm/deal_assets_service.py
git commit -m "feat(deal-assets): add DealAssetsService with CRUD + presign flow"
```

---

## Task 5: Create the API Handler

**Files:**

- Create: `src/api/routers/crm/deal_assets_handler.py`
- Modify: `src/api/routers/crm/__init__.py`
- Modify: `src/api/fastapi_app.py`

**Step 1: Create the handler file**

```python
"""Deal Assets API — manage files and external links associated with a deal."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.deal_asset_models import (
    DealAssetFinalizeRequest,
    DealAssetItem,
    DealAssetLinkCreateRequest,
    DealAssetPresignRequest,
    DealAssetPresignResponse,
    DealAssetUpdateRequest,
    DealAssetsPage,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.deal_assets_service import (
    DealAssetsService,
    DealAssetsServiceError,
)

logger = get_logger("deal_assets_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deal_assets_router = APIRouter()


# ── Dependency ───────────────────────────────────────────────────────────


async def get_deal_assets_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealAssetsService for the current org."""
    service = DealAssetsService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealAssetsService close failed", exc_info=True)


# ── Helpers ──────────────────────────────────────────────────────────────


async def _map_asset_to_item(
    asset, service: DealAssetsService
) -> DealAssetItem:
    """Map a DealAsset ORM object to the API response schema.

    Generates short-lived SAS URLs for files and thumbnails.
    """
    download_url = await service.generate_download_url(asset)
    thumb_small, thumb_medium = await service.generate_thumbnail_urls(asset)

    return DealAssetItem(
        asset_id=asset.asset_id,
        deal_id=asset.deal_id,
        name=asset.name,
        category=asset.category,
        direction=asset.direction,
        status=asset.status,
        filename=asset.filename,
        mimetype=asset.mimetype,
        size_bytes=asset.size_bytes,
        download_url=download_url,
        thumbnail_small_url=thumb_small,
        thumbnail_medium_url=thumb_medium,
        external_url=asset.external_url,
        external_source=asset.external_source,
        link_status=asset.link_status,
        link_checked_at=asset.link_checked_at,
        kb_source_id=asset.kb_source_id,
        source_type=asset.source_type,
        source_context=asset.source_context,
        deal_stage_when_added=asset.deal_stage_when_added,
        added_by_user_id=asset.added_by_user_id,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


# ── Endpoints ────────────────────────────────────────────────────────────


@deal_assets_router.get(
    "/{deal_id}/assets",
    response_model=GenericResponse[DealAssetsPage],
    summary="List deal assets",
    description="Retrieve all files and links for a deal",
    dependencies=[Depends(rate_limit_user_read)],
)
async def list_deal_assets(
    deal_id: UUID,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
) -> GenericResponse[DealAssetsPage]:
    try:
        assets, total_count = await service.list_assets(deal_id)
        items = [await _map_asset_to_item(a, service) for a in assets]
        return Response.success_response(
            message="Deal assets retrieved successfully",
            data=DealAssetsPage(items=items, total_count=total_count),
        )
    except DealAssetsServiceError as exc:
        logger.warning("Deal assets list error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing assets for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.get(
    "/{deal_id}/assets/{asset_id}",
    response_model=GenericResponse[DealAssetItem],
    summary="Get deal asset",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_deal_asset(
    deal_id: UUID,
    asset_id: UUID,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
) -> GenericResponse[DealAssetItem]:
    try:
        asset = await service.get_asset(deal_id, asset_id)
        item = await _map_asset_to_item(asset, service)
        return Response.success_response(message="Asset retrieved successfully", data=item)
    except DealAssetsServiceError as exc:
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error getting asset %s: %s", asset_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.post(
    "/{deal_id}/assets/upload/presign",
    response_model=GenericResponse[DealAssetPresignResponse],
    status_code=201,
    summary="Presign file upload",
    description="Create an asset record and return a presigned upload URL",
)
async def presign_asset_upload(
    deal_id: UUID,
    request: DealAssetPresignRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetPresignResponse]:
    try:
        asset, upload_url, expires = await service.presign_file_upload(
            deal_id=deal_id,
            user_id=user_ids.user_id,
            filename=request.filename,
            mimetype=request.mimetype,
            size_bytes=request.size_bytes,
            name=request.name,
            category=request.category,
            direction=request.direction,
        )
        await service.session.commit()
        return Response.created_response(
            message="Presigned URL generated",
            data=DealAssetPresignResponse(
                asset_id=asset.asset_id,
                filename=asset.filename,
                upload_url=upload_url,
                expires_in_seconds=expires,
                status=asset.status,
            ),
            response=response,
        )
    except DealAssetsServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Presign error for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.post(
    "/{deal_id}/assets/upload/finalize",
    response_model=GenericResponse[DealAssetItem],
    summary="Finalize file upload",
    description="Confirm upload completed and transition asset to active",
)
async def finalize_asset_upload(
    deal_id: UUID,
    request: DealAssetFinalizeRequest,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    try:
        asset = await service.finalize_file_upload(
            deal_id=deal_id,
            asset_id=request.asset_id,
            checksum=request.checksum,
        )
        await service.session.commit()
        item = await _map_asset_to_item(asset, service)
        return Response.success_response(message="Upload finalized successfully", data=item)
    except DealAssetsServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Finalize error for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.post(
    "/{deal_id}/assets/links",
    response_model=GenericResponse[DealAssetItem],
    status_code=201,
    summary="Add external link",
    description="Add an external link (Drive, SharePoint, Confluence, etc.) to the deal",
)
async def create_deal_asset_link(
    deal_id: UUID,
    request: DealAssetLinkCreateRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    try:
        asset = await service.create_link(
            deal_id=deal_id,
            user_id=user_ids.user_id,
            name=request.name,
            external_url=request.external_url,
            external_source=request.external_source,
            category=request.category,
            direction=request.direction,
        )
        await service.session.commit()
        item = await _map_asset_to_item(asset, service)
        return Response.created_response(
            message="External link added successfully", data=item, response=response
        )
    except DealAssetsServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Link creation error for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.patch(
    "/{deal_id}/assets/{asset_id}",
    response_model=GenericResponse[DealAssetItem],
    summary="Update deal asset",
)
async def update_deal_asset(
    deal_id: UUID,
    asset_id: UUID,
    request: DealAssetUpdateRequest,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    try:
        asset = await service.update_asset(
            deal_id=deal_id,
            asset_id=asset_id,
            updates=request.model_dump(exclude_unset=True),
        )
        await service.session.commit()
        item = await _map_asset_to_item(asset, service)
        return Response.success_response(message="Asset updated successfully", data=item)
    except DealAssetsServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Update error for asset %s: %s", asset_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.delete(
    "/{deal_id}/assets/{asset_id}",
    response_model=GenericResponse[DealAssetItem],
    summary="Delete deal asset",
    description="Soft-delete a deal asset",
)
async def delete_deal_asset(
    deal_id: UUID,
    asset_id: UUID,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    try:
        asset = await service.delete_asset(deal_id=deal_id, asset_id=asset_id)
        await service.session.commit()
        item = await _map_asset_to_item(asset, service)
        return Response.success_response(message="Asset deleted successfully", data=item)
    except DealAssetsServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Delete error for asset %s: %s", asset_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
```

**Step 2: Register the router in `src/api/routers/crm/__init__.py`**

Add import and export:

```python
# Add import:
from .deal_assets_handler import deal_assets_router

# Add to __all__:
    "deal_assets_router",
```

**Step 3: Register the router in `src/api/fastapi_app.py`**

Add import at the top (inside the existing `from src.api.routers.crm import (...)` block):

```python
    deal_assets_router,
```

Add router registration after the `deal_notes_router` line (~line 193):

```python
    fastapp.include_router(
        deal_assets_router, prefix="/users/crm/deals", tags=["deal-assets"]
    )
```

**Step 4: Verify the app starts without import errors**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run python -c "from src.api.fastapi_app import create_app; print('OK')"`

Expected: `OK`

**Step 5: Commit**

```bash
git add src/api/routers/crm/deal_assets_handler.py src/api/routers/crm/__init__.py src/api/fastapi_app.py
git commit -m "feat(deal-assets): add API handler with CRUD + upload endpoints"
```

---

## Task 6: Write Tests for the Service Layer

**Files:**

- Create: `tests/service/crm/test_deal_assets_service.py`

**Step 1: Create the test file**

Tests should cover:

1. `list_assets` — returns only active, non-deleted assets for the correct deal
2. `get_asset` — returns asset when found, raises 404 when missing
3. `presign_file_upload` — creates pending_upload record with correct storage_path
4. `finalize_file_upload` — transitions pending → active, rejects double-finalize
5. `create_link` — creates active link asset with correct fields
6. `update_asset` — applies partial updates
7. `delete_asset` — soft-deletes

```python
"""Tests for DealAssetsService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetStatus,
)
from src.service.crm.deal_assets_service import (
    DealAssetsService,
    DealAssetsServiceError,
)


@pytest.fixture
def org_id():
    return uuid4()


@pytest.fixture
def deal_id():
    return uuid4()


@pytest.fixture
def user_id():
    return uuid4()


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def service(mock_session, org_id):
    return DealAssetsService(mock_session, org_id)


class TestPresignFileUpload:
    @pytest.mark.asyncio
    @patch("src.service.crm.deal_assets_service.AzureOrganisationBlobStorage")
    async def test_creates_pending_asset_and_returns_sas_url(
        self, mock_blob_cls, service, deal_id, user_id
    ):
        mock_blob = AsyncMock()
        mock_blob.generate_write_sas.return_value = "https://blob.test/sas"
        mock_blob_cls.return_value = mock_blob

        asset, url, expires = await service.presign_file_upload(
            deal_id=deal_id,
            user_id=user_id,
            filename="proposal.pdf",
            mimetype="application/pdf",
            size_bytes=1024,
            name="Proposal v1",
            category="proposal",
            direction="shared",
        )

        assert asset.status == DealAssetStatus.PENDING_UPLOAD.value
        assert asset.filename == "proposal.pdf"
        assert asset.name == "Proposal v1"
        assert asset.direction == "shared"
        assert "deal_assets/" in asset.storage_path
        assert url == "https://blob.test/sas"
        assert expires == 15 * 60
        service.session.add.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.service.crm.deal_assets_service.AzureOrganisationBlobStorage")
    async def test_defaults_name_to_filename(self, mock_blob_cls, service, deal_id, user_id):
        mock_blob_cls.return_value = AsyncMock(
            generate_write_sas=AsyncMock(return_value="https://blob.test/sas")
        )

        asset, _, _ = await service.presign_file_upload(
            deal_id=deal_id,
            user_id=user_id,
            filename="doc.pdf",
            mimetype="application/pdf",
            size_bytes=512,
        )
        assert asset.name == "doc.pdf"


class TestFinalizeFileUpload:
    @pytest.mark.asyncio
    async def test_transitions_pending_to_active(self, service, deal_id):
        asset_id = uuid4()
        pending_asset = DealAsset(
            asset_id=asset_id,
            organisation_id=service.organisation_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="test.pdf",
            status=DealAssetStatus.PENDING_UPLOAD.value,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = pending_asset
        service.session.execute.return_value = mock_result

        result = await service.finalize_file_upload(deal_id, asset_id, checksum="abc123")

        assert result.status == DealAssetStatus.ACTIVE.value
        assert result.checksum == "abc123"

    @pytest.mark.asyncio
    async def test_rejects_already_active(self, service, deal_id):
        asset_id = uuid4()
        active_asset = DealAsset(
            asset_id=asset_id,
            organisation_id=service.organisation_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="test.pdf",
            status=DealAssetStatus.ACTIVE.value,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = active_asset
        service.session.execute.return_value = mock_result

        with pytest.raises(DealAssetsServiceError) as exc_info:
            await service.finalize_file_upload(deal_id, asset_id)

        assert exc_info.value.status_code == 409


class TestCreateLink:
    @pytest.mark.asyncio
    async def test_creates_active_link_asset(self, service, deal_id, user_id):
        asset = await service.create_link(
            deal_id=deal_id,
            user_id=user_id,
            name="RFP Document",
            external_url="https://drive.google.com/file/abc",
            external_source="google_drive",
            category="rfp",
            direction="received",
        )

        assert asset.status == DealAssetStatus.ACTIVE.value
        assert asset.external_url == "https://drive.google.com/file/abc"
        assert asset.external_source == "google_drive"
        assert asset.direction == "received"
        assert asset.storage_path is None
        service.session.add.assert_called_once()


class TestGetAsset:
    @pytest.mark.asyncio
    async def test_raises_404_when_not_found(self, service, deal_id):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        service.session.execute.return_value = mock_result

        with pytest.raises(DealAssetsServiceError) as exc_info:
            await service.get_asset(deal_id, uuid4())

        assert exc_info.value.status_code == 404


class TestDeleteAsset:
    @pytest.mark.asyncio
    async def test_soft_deletes_asset(self, service, deal_id):
        asset = DealAsset(
            asset_id=uuid4(),
            organisation_id=service.organisation_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="old.pdf",
            status=DealAssetStatus.ACTIVE.value,
            is_deleted=False,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = asset
        service.session.execute.return_value = mock_result

        result = await service.delete_asset(deal_id, asset.asset_id)

        assert result.is_deleted is True
```

**Step 2: Run the tests**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run pytest tests/service/crm/test_deal_assets_service.py -v`

Expected: All tests pass.

**Step 3: Commit**

```bash
git add tests/service/crm/test_deal_assets_service.py
git commit -m "test(deal-assets): add service layer unit tests"
```

---

## Task 7: Write Tests for the API Handler

**Files:**

- Create: `tests/api/routers/crm/test_deal_assets_handler.py`

**Step 1: Create the test file**

Tests should cover:

1. `GET /{deal_id}/assets` — lists assets
2. `POST /{deal_id}/assets/upload/presign` — returns presigned URL
3. `POST /{deal_id}/assets/upload/finalize` — transitions status
4. `POST /{deal_id}/assets/links` — creates external link
5. `PATCH /{deal_id}/assets/{asset_id}` — partial update
6. `DELETE /{deal_id}/assets/{asset_id}` — soft delete
7. Error cases: 404, 409

Use the same test patterns as `tests/api/routers/chat_attachments/test_chat_attachments_upload_handler.py` — check that file first for the exact fixture patterns and test client setup used in this project, then mirror them. The fixtures, auth mocking, and `TestClient` setup vary by project.

**Step 2: Run the tests**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run pytest tests/api/routers/crm/test_deal_assets_handler.py -v`

Expected: All tests pass.

**Step 3: Commit**

```bash
git add tests/api/routers/crm/test_deal_assets_handler.py
git commit -m "test(deal-assets): add API handler integration tests"
```

---

## Task 8: Add API Documentation

**Files:**

- Create: `docs/API Documentation/deals/deal-assets-api.md`

**Step 1: Create the documentation file**

Document all endpoints following the pattern in `docs/API Documentation/deals/deal-notes-api.md`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/{deal_id}/assets` | GET | List all assets (files + links) for a deal |
| `/{deal_id}/assets/{asset_id}` | GET | Get a single asset |
| `/{deal_id}/assets/upload/presign` | POST | Get a presigned upload URL |
| `/{deal_id}/assets/upload/finalize` | POST | Confirm upload completed |
| `/{deal_id}/assets/links` | POST | Add an external link |
| `/{deal_id}/assets/{asset_id}` | PATCH | Update asset metadata |
| `/{deal_id}/assets/{asset_id}` | DELETE | Soft-delete an asset |

Include request/response schemas, validation rules, error codes, and example payloads.

**Step 2: Commit**

```bash
git add docs/API\ Documentation/deals/deal-assets-api.md
git commit -m "docs(deal-assets): add API documentation"
```

---

## Task 9: Run Linting and Type Checks

**Step 1: Run ruff**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run ruff check src/domain/models/organisation_tables/deal_asset_models.py src/service/crm/deal_assets_service.py src/api/routers/crm/deal_assets_handler.py src/api/schemas/deal_asset_models.py`

Fix any issues found.

**Step 2: Run mypy**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run mypy src/domain/models/organisation_tables/deal_asset_models.py src/service/crm/deal_assets_service.py src/api/routers/crm/deal_assets_handler.py src/api/schemas/deal_asset_models.py`

Fix any issues found.

**Step 3: Run all tests**

Run: `cd D:/CopilotGTM/copilot-backend-deal-assets && uv run pytest tests/service/crm/test_deal_assets_service.py tests/api/routers/crm/test_deal_assets_handler.py -v`

Expected: All tests pass.

**Step 4: Commit any fixes**

```bash
git add -u
git commit -m "fix(deal-assets): lint and type-check fixes"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | DealAsset model + enums | `deal_asset_models.py`, `__init__.py` |
| 2 | Alembic migration (030) | `3c7f79637798_030_add_deal_assets.py` |
| 3 | Pydantic schemas | `deal_asset_models.py` (schemas) |
| 4 | DealAssetsService | `deal_assets_service.py` |
| 5 | API handler + router registration | `deal_assets_handler.py`, `__init__.py`, `fastapi_app.py` |
| 6 | Service tests | `test_deal_assets_service.py` |
| 7 | API handler tests | `test_deal_assets_handler.py` |
| 8 | API documentation | `deal-assets-api.md` |
| 9 | Lint + type check | All new files |

### API Endpoints (prefix: `/users/crm/deals`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/{deal_id}/assets` | List all deal assets |
| GET | `/{deal_id}/assets/{asset_id}` | Get single asset |
| POST | `/{deal_id}/assets/upload/presign` | Get presigned upload URL |
| POST | `/{deal_id}/assets/upload/finalize` | Confirm upload done |
| POST | `/{deal_id}/assets/links` | Add external link |
| PATCH | `/{deal_id}/assets/{asset_id}` | Update asset |
| DELETE | `/{deal_id}/assets/{asset_id}` | Soft-delete asset |

### Blob Storage Path

```
Container: org_{organisation_id}
Path:      deal_assets/{user_id}/{asset_id}/{filename}
```
