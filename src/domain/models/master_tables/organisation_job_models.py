"""Organisation Job models for tracking async organisation operations.

These jobs are stored in the master database (public schema) since organisations
are master-level entities. The job pattern enables:
- Immediate API responses with job_id
- Real-time progress updates via SSE
- Retry visibility and error tracking
- Audit trail for organisation operations
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from .base_mixins import TimestampMixin


class OrganisationJobType(str, Enum):
    """Types of organisation-related jobs."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    MIGRATE = "migrate"


class OrganisationJobStatus(str, Enum):
    """Status of an organisation job."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrganisationJob(TimestampMixin, table=True):
    """
    Tracks async jobs for organisation lifecycle operations.

    Stored in master DB (public schema) since organisations are master-level entities.
    """

    __tablename__ = "organisation_jobs"
    __table_args__ = (
        Index("ix_organisation_jobs_admin_created", "admin_id", "created_at"),
        Index("ix_organisation_jobs_status", "status"),
        {"schema": "public"},
    )

    job_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique job identifier (returned to client immediately).",
    )

    # Job context
    job_type: OrganisationJobType = Field(
        sa_column=Column(String(50), nullable=False),
        description="Type of operation (create, update, delete, migrate).",
    )
    admin_id: UUID = Field(
        index=True,
        description="Admin who initiated the job.",
    )
    organisation_id: UUID | None = Field(
        default=None,
        index=True,
        description="Target organisation (null for create until org exists).",
    )
    organisation_name: str | None = Field(
        default=None,
        max_length=255,
        description="Organisation name (for create jobs before org record exists).",
    )

    # Status tracking
    status: OrganisationJobStatus = Field(
        default=OrganisationJobStatus.PENDING,
        sa_column=Column(String(50), server_default=text("'pending'"), nullable=False),
        description="Current job status.",
    )
    progress: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Progress percentage (0-100).",
    )
    status_message: str | None = Field(
        default=None,
        max_length=500,
        description="Human-readable status message.",
    )

    # Result/error storage
    result_data: dict[str, Any] | None = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Result payload on success (organisation details).",
    )
    error_message: str | None = Field(
        sa_column=Column(Text),
        default=None,
        description="Error message on failure.",
    )
    error_code: str | None = Field(
        default=None,
        max_length=100,
        description="Error code for programmatic handling.",
    )

    # Completion tracking
    completed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        description="Timestamp when job reached terminal state (completed/failed/cancelled).",
    )

    # Retry tracking
    attempt_count: int = Field(
        default=0,
        description="Number of processing attempts.",
    )
    max_retries: int = Field(
        default=3,
        description="Maximum retry attempts.",
    )

    # Request metadata (for audit/replay)
    request_payload: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
        description="Original request payload (for retry/audit).",
    )
