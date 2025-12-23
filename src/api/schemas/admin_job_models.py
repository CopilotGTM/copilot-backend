"""Pydantic models for organisation job SSE events and API responses.

These DTOs define the structure for:
- SSE event payloads streamed to frontend
- API response models for job status queries
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from src.domain.models.master_tables.organisation_job_models import (
    OrganisationJobStatus,
    OrganisationJobType,
)


class OrganisationJobEventUpdate(BaseModel):
    """SSE event payload for organisation job status updates.

    Streamed to frontend via SSE when job status changes.
    """

    job_id: UUID
    status: OrganisationJobStatus
    progress: int = Field(ge=0, le=100, description="Progress percentage 0-100")
    status_message: str | None = None
    result_data: dict[str, Any] | None = None
    error_message: str | None = None
    error_code: str | None = None
    updated_at: datetime
    completed_at: datetime | None = None


class OrganisationJobResponse(BaseModel):
    """Full organisation job details for API responses."""

    job_id: UUID
    job_type: OrganisationJobType
    admin_id: UUID
    organisation_id: UUID | None = None
    status: OrganisationJobStatus
    progress: int = Field(ge=0, le=100)
    status_message: str | None = None
    result_data: dict[str, Any] | None = None
    error_message: str | None = None
    error_code: str | None = None
    attempt_count: int = 0
    max_retries: int = 3
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    class Config:
        from_attributes = True


class OrganisationJobCreateResponse(BaseModel):
    """Response for job creation (202 Accepted).

    Note: Use `stream_url` for real-time SSE updates. Polling is deprecated.
    """

    job_id: UUID
    job_type: OrganisationJobType
    status: OrganisationJobStatus = OrganisationJobStatus.PENDING
    message: str = "Job accepted for processing"
    stream_url: str = Field(description="URL to stream job updates via SSE (preferred)")


class OrganisationJobListResponse(BaseModel):
    """Response for listing organisation jobs."""

    jobs: list[OrganisationJobResponse]
    total: int
    page: int = 1
    page_size: int = 20
