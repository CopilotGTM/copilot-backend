from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl


class KnowledgeSourceListItem(BaseModel):
    source_id: UUID
    filename: str | None
    mimetype: str | None
    size_bytes: int | None
    status: str
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None
    checksum: str | None = None
    thumbnail_small_url: HttpUrl | None = None
    thumbnail_medium_url: HttpUrl | None = None

    model_config = {"from_attributes": True}


class KnowledgeSourcesPage(BaseModel):
    items: list[KnowledgeSourceListItem]
    cursor: str | None = None


class KnowledgeDownloadResponse(BaseModel):
    source_id: UUID
    download_url: HttpUrl
    expires_in_seconds: int = Field(default=120 * 60)


class KnowledgeSourcesStats(BaseModel):
    total: int
    by_status: dict[str, int]
    by_type: dict[str, int]


# -------- Delete Models --------
class KnowledgeBatchDeleteRequest(BaseModel):
    """Request body for deletion of one or more knowledge sources."""

    source_ids: list[UUID] = Field(..., min_length=1, max_length=100)
    client_batch_id: str | None = Field(default=None, max_length=100)


class KnowledgeDeleteResultItem(BaseModel):
    """Per-source result in batch deletion response."""

    source_id: UUID
    status: str  # "deleting", "already_deleted", "not_found"
    job_id: UUID | None = None
    error_message: str | None = None


class KnowledgeBatchDeleteResponse(BaseModel):
    """Response for batch deletion request."""

    batch_id: UUID
    results: list[KnowledgeDeleteResultItem]
    errors: list[dict[str, Any]] = []
