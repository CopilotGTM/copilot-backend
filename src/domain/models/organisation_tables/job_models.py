"""Persistent job and workqueue models for async operations.

This module owns shared job metadata and status contracts used by worker code
and task orchestration. The enum values represent org-level processing paths
that must remain stable because delayed workers may deserialize historic job_type
values long after feature changes land.
"""

from decimal import Decimal
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Column, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from .base_mixins import TimestampMixin
from .tenant_constraints import tenant_foreign_key

if TYPE_CHECKING:
    from .chat_message_models import ChatMessage
    from .knowledge_models import OrganisationSource

# SQL server defaults - avoid duplication across models
_DEFAULT_STATUS_PENDING = text("'pending'")
_DEFAULT_EMPTY_JSONB = text("'{}'::jsonb")


class JobStatus(str, Enum):
    """
    Enum for the status of a job.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChatMessageJob(TimestampMixin, table=True):
    """
    Represents a job associated with a chat message.
    """

    __tablename__ = "chat_message_jobs"
    __table_args__ = (
        tenant_foreign_key(
            "chat_message_jobs",
            "message_id",
            "chat_messages",
            "message_id",
        ),
        Index(
            "uq_chat_message_jobs_channel_idempotency_org",
            "organisation_id",
            "chat_type",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_chat_message_jobs_recovery",
            "chat_type",
            "status",
            "updated_at",
            postgresql_where=text("status IN ('pending', 'in_progress', 'failed')"),
        ),
    )

    job_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the job.",
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
    message_id: UUID | None = Field(
        default=None,
        index=True,
        description="Foreign key linking to the chat message. Nullable for group/channel bot chats.",
    )
    job_type: str = Field(
        max_length=100,
        description="The type of job (e.g., 'general_message', 'teams_chat_response').",
    )
    chat_type: str = Field(
        default="GENERAL_CHAT",
        sa_column=Column(
            String(50), server_default=text("'GENERAL_CHAT'"), nullable=False, index=True
        ),
        description="Source channel: GENERAL_CHAT, TEAMS_CHAT, SLACK_CHAT (mirrors ChatType enum).",
    )
    status: JobStatus = Field(
        default=JobStatus.PENDING,
        sa_column=Column(String(50), server_default=text("'pending'"), nullable=False),
        description="The current status of the job.",
    )
    idempotency_key: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable provider retry identity, unique within a chat channel.",
    )
    job_metadata: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=text("'{}'::jsonb")),
        default_factory=dict,
        description="Additional metadata for the job.",
    )
    result_data: dict[str, Any] | None = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Result data of the job upon completion or failure.",
    )
    error_message: str | None = Field(
        default=None,
        description="Error message if the job failed.",
    )
    user_id: UUID | None = Field(
        default=None,
        index=True,
        description="User who initiated the job (for per-user cost tracking).",
    )
    # Token tracking
    token_usage: dict[str, Any] | None = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Token usage metrics from the agent run.",
    )
    model_name: str | None = Field(
        default=None,
        max_length=100,
        description="The model that processed this job.",
    )
    cost: Decimal | None = Field(
        sa_column=Column(Numeric),
        default=None,
        description="Total USD cost of the LLM call.",
    )
    latency_ms: int | None = Field(
        sa_column=Column(Integer),
        default=None,
        description="End-to-end LLM call duration in milliseconds.",
    )

    # Relationship to ChatMessage (optional — group/channel bot chats have no message)
    message: "ChatMessage" = Relationship(
        back_populates="jobs",
        sa_relationship_kwargs={"foreign_keys": "[ChatMessageJob.message_id]"},
    )


ChatMessageJob.update_forward_refs()


class KnowledgeProcessingJob(TimestampMixin, table=True):
    """
    Represents a job for processing a knowledge source.
    """

    __tablename__ = "knowledge_processing_jobs"
    __table_args__ = (
        tenant_foreign_key(
            "knowledge_processing_jobs",
            "source_id",
            "organisation_sources",
            "source_id",
        ),
    )

    job_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the job.",
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
    source_id: UUID = Field(
        index=True,
        description="Foreign key linking to the knowledge source.",
    )
    job_type: str = Field(
        max_length=100,
        description="The type of job (e.g., 'chunk_file', 'sync_integration').",
    )
    status: JobStatus = Field(
        default=JobStatus.PENDING,
        sa_column=Column(String(50), server_default=_DEFAULT_STATUS_PENDING, nullable=False),
        description="The current status of the job.",
    )
    job_metadata: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=_DEFAULT_EMPTY_JSONB),
        default_factory=dict,
        description="Additional metadata for the job.",
    )
    result_data: dict[str, Any] | None = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Result data of the job upon completion or failure.",
    )
    error_message: str | None = Field(
        default=None,
        description="Error message if the job failed.",
    )

    # Relationship to OrganisationSource
    source: "OrganisationSource" = Relationship(back_populates="jobs")


KnowledgeProcessingJob.update_forward_refs()


class DealJobType(StrEnum):
    """Types of jobs that can be run for a deal."""

    # Meeting-related jobs
    MEETING_BRIEFING = "meeting_briefing"  # Pre-meeting prep briefing
    MEETING_SUMMARY = "meeting_summary"  # Post-meeting summary
    MEETING_SUMMARY_NOTIFICATION = "meeting_summary_notification"  # Summary-ready notification
    MEETING_FOLLOWUP = "meeting_followup"  # Follow-up action items

    # Stakeholder-related jobs
    STAKEHOLDER_ANALYSIS = "stakeholder_analysis"  # Deep stakeholder analysis
    STAKEHOLDER_ENRICHMENT = "stakeholder_enrichment"  # Enrich stakeholder data
    DEAL_EMAIL_REMATCH = "deal_email_rematch"  # Re-match deal emails after stakeholder changes
    DEAL_CALENDAR_REMATCH = "deal_calendar_rematch"  # Re-match meetings after stakeholder changes
    STAKEHOLDER_SYNC = "stakeholder_sync"  # Sync stakeholder set after stakeholder changes

    # Deal-related jobs
    DEAL_SUMMARY = "deal_summary"  # Deal overview summary
    DEAL_UPDATE = "deal_update"  # AI-assisted internal CRM deal update
    DEAL_COMMUNICATION_HYDRATION = "deal_communication_hydration"
    EMAIL_ATTACHMENT_EXTRACTION = "email_attachment_extraction"
    DEAL_RISK_ANALYSIS = "deal_risk_analysis"  # Risk assessment
    DEAL_NEXT_STEPS = "deal_next_steps"  # Recommended next steps

    # Risk & Blocker extraction
    RISK_BLOCKER_EXTRACTION = "risk_blocker_extraction"  # AI extraction of risks/blockers

    # Content generation jobs
    PROPOSAL_DRAFT = "proposal_draft"  # Draft proposal content
    EMAIL_DRAFT = "email_draft"  # Draft follow-up email
    # Transcript upload
    TRANSCRIPT_UPLOAD = "transcript_upload"  # Manual transcript file upload + extraction

    # Deal memory
    DEAL_MEMORY_BUILD = "deal_memory_build"  # Build or update deal memory document
    DEAL_TASK_SYNC = "deal_task_sync"  # Create/complete deal tasks from meeting insights
    CALENDAR_CONTACT_ACCOUNT_DISCOVERY = (
        "calendar_contact_account_discovery"  # Legacy value retained for persisted job rows
    )
    CRM_CSV_IMPORT = "crm_csv_import"

    CONTACT_RESEARCH = "contact_research"
    ACCOUNT_RESEARCH = "account_research"


class DealJobs(TimestampMixin, table=True):
    """
    Job model for deal-related async operations.

    Supports multiple job types that can run in parallel.
    Each job is associated with a deal and optionally with an event/meeting.
    """

    __tablename__ = "deal_jobs"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "job_id",
            name="uq_deal_jobs_org_job_id",
        ),
        tenant_foreign_key("deal_jobs", "deal_id", "crm_deals", "deal_id"),
        tenant_foreign_key(
            "deal_jobs",
            "event_id",
            "calendar_events",
            "event_id",
        ),
        Index("ix_deal_jobs_org_deal", "organisation_id", "deal_id"),
        Index("ix_deal_jobs_org_status", "organisation_id", "status"),
        Index("ix_deal_jobs_type_status", "job_type", "status"),
        Index(
            "ix_deal_jobs_active_lookup",
            "organisation_id",
            "deal_id",
            "job_type",
            "event_id",
            "status",
        ),
    )

    job_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    user_id: UUID = Field(index=True, description="User who initiated the job")

    # Job type discriminator
    job_type: DealJobType = Field(
        sa_column=Column(String(50), nullable=False, index=True),
        description="Type of job being executed",
    )

    # Associations (nullable - depends on job type)
    deal_id: UUID | None = Field(
        default=None,
        index=True,
        description="The deal this job is associated with (nullable for org-level jobs)",
    )
    event_id: UUID | None = Field(
        default=None,
        index=True,
        description="Optional: The calendar event (for meeting-related jobs)",
    )
    stakeholder_id: UUID | None = Field(
        default=None,
        index=True,
        description=(
            "Optional: Specific stakeholder for stakeholder-related jobs. During the "
            "participant-backed transition this stores CRMDealParticipant.participant_id."
        ),
    )

    # Job state
    status: JobStatus = Field(
        default=JobStatus.PENDING,
        sa_column=Column(String(50), server_default=_DEFAULT_STATUS_PENDING, nullable=False),
    )

    # Flexible metadata for job-specific inputs
    job_metadata: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=_DEFAULT_EMPTY_JSONB),
        default_factory=dict,
        description="Job-specific input parameters and context",
    )

    # Result storage
    result_data: dict[str, Any] | None = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Job output/result data",
    )

    # Error handling
    error_message: str | None = Field(
        default=None,
        description="Error message if job failed",
    )
    retry_count: int = Field(
        default=0,
        description="Number of retry attempts",
    )

    # Token tracking
    token_usage: dict[str, Any] | None = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Token usage metrics from the agent run.",
    )
    model_name: str | None = Field(
        default=None,
        max_length=100,
        description="The model that processed this job.",
    )
    cost: Decimal | None = Field(
        sa_column=Column(Numeric),
        default=None,
        description="Total USD cost of the LLM call.",
    )
    latency_ms: int | None = Field(
        sa_column=Column(Integer),
        default=None,
        description="End-to-end LLM call duration in milliseconds.",
    )


DealJobs.update_forward_refs()
