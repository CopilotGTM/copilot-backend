"""Pydantic request/response schemas for the Deal Assets API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# ── Request Schemas ──────────────────────────────────────────────────────────


class DealAssetPresignRequest(BaseModel):
    """Request a presigned URL to upload a file asset to a deal."""

    filename: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Original filename of the asset.",
    )
    mimetype: str = Field(..., max_length=255, description="MIME type of the file.")
    size_bytes: int = Field(..., gt=0, description="File size in bytes.")
    name: str | None = Field(
        default=None,
        max_length=500,
        description="Display name for the asset. Defaults to filename if omitted.",
    )
    category: str | None = Field(
        default=None,
        max_length=100,
        description="Asset category (e.g. proposal, contract).",
    )
    direction: str = Field(
        default="internal",
        pattern=r"^(received|shared|internal)$",
        description="Direction of the asset: received, shared, or internal.",
    )

    @field_validator("filename")
    @classmethod
    def strip_filename(cls, v: str) -> str:
        return v.strip()


class DealAssetFinalizeRequest(BaseModel):
    """Confirm that a previously presigned asset upload has completed."""

    asset_id: UUID = Field(..., description="The asset ID returned by the presign endpoint.")
    checksum: str | None = Field(
        default=None,
        max_length=128,
        description="Optional checksum (MD5/SHA-256) for integrity verification.",
    )


# ── Response Schemas ─────────────────────────────────────────────────────────


class DealAssetItem(BaseModel):
    """Single deal asset in API responses."""

    asset_id: UUID
    deal_id: UUID
    name: str
    category: str | None
    direction: str
    status: str
    filename: str | None
    mimetype: str | None
    size_bytes: int | None
    download_url: str | None = Field(default=None, description="Time-limited SAS download URL.")
    thumbnail_small_url: str | None = Field(
        default=None, description="Time-limited SAS URL for small thumbnail."
    )
    thumbnail_medium_url: str | None = Field(
        default=None, description="Time-limited SAS URL for medium thumbnail."
    )
    kb_source_id: UUID | None
    source_type: str
    source_context: str | None
    deal_stage_when_added: str | None
    added_by_user_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DealAssetPresignResponse(BaseModel):
    """Response after generating a presigned upload URL for a deal asset."""

    asset_id: UUID
    filename: str
    upload_url: str
    expires_in_seconds: int = Field(..., description="Seconds until the upload URL expires.")
    status: str


class DealAssetsPage(BaseModel):
    """Paginated list of deal assets."""

    items: list[DealAssetItem] = Field(description="List of deal assets.")
    total_count: int = Field(description="Total number of assets for this deal.")
    next_cursor: str | None = Field(
        default=None,
        description="Cursor for the next page, or null when this is the final page.",
    )
