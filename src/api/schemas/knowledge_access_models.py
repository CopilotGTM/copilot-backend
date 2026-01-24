from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSourceStatus,
    SourceType,
)

# Statuses considered "active" (require SSE monitoring)
ACTIVE_STATUSES = {"pending_upload", "uploaded", "processing", "extracting", "deleting"}


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
    is_active: bool = False  # True if source needs SSE monitoring (status in ACTIVE_STATUSES)

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
    active_count: int = 0  # Count of sources needing SSE monitoring
    recent_failures_24h: int = 0  # Failed sources in last 24 hours


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


class KnowledgeSourcesListParams(NamedTuple):
    """Parsed query parameters for listing knowledge sources.

    Note: This is a NamedTuple (not Pydantic BaseModel) because FastAPI only properly
    parses list query parameters (like statuses, source_types) when Query() is in a
    function signature. Use the get_knowledge_sources_list_params() dependency function.
    """

    limit: int
    cursor: str | None
    statuses: list[OrganisationSourceStatus] | None
    source_types: list[SourceType] | None
    q: str | None
    updated_after: datetime | None
    include_urls: bool
