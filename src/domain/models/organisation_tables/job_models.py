from enum import Enum
from uuid import UUID, uuid4
from typing import Optional, Dict, Any, TYPE_CHECKING
from datetime import datetime

from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .chat_message_models import ChatMessage
    from .knowledge_models import OrganisationSource


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

    job_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the job.",
    )
    message_id: UUID = Field(
        foreign_key="chat_messages.message_id",
        index=True,
        description="Foreign key linking to the chat message.",
    )
    job_type: str = Field(
        max_length=100,
        description="The type of job (e.g., 'technical_deck_generation', 'rfp_sheet_processing').",
    )
    status: JobStatus = Field(
        default=JobStatus.PENDING,
        sa_column=Column(String(50)),
        description="The current status of the job.",
    )
    job_metadata: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
        description="Additional metadata for the job.",
    )
    result_data: Optional[Dict[str, Any]] = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Result data of the job upon completion or failure.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error message if the job failed.",
    )

    # Relationship to ChatMessage
    message: "ChatMessage" = Relationship(back_populates="jobs")


ChatMessageJob.update_forward_refs()


class KnowledgeProcessingJob(TimestampMixin, table=True):
    """
    Represents a job for processing a knowledge source.
    """

    __tablename__ = "knowledge_processing_jobs"

    job_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the job.",
    )
    source_id: UUID = Field(
        foreign_key="organisation_sources.source_id",
        index=True,
        description="Foreign key linking to the knowledge source.",
    )
    job_type: str = Field(
        max_length=100,
        description="The type of job (e.g., 'chunk_file', 'sync_integration').",
    )
    status: JobStatus = Field(
        default=JobStatus.PENDING,
        sa_column=Column(String(50)),
        description="The current status of the job.",
    )
    job_metadata: Dict[str, Any] = Field(
        sa_column=Column(JSONB),
        default_factory=dict,
        description="Additional metadata for the job.",
    )
    result_data: Optional[Dict[str, Any]] = Field(
        sa_column=Column(JSONB),
        default=None,
        description="Result data of the job upon completion or failure.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error message if the job failed.",
    )

    # Relationship to OrganisationSource
    source: "OrganisationSource" = Relationship(back_populates="jobs")


KnowledgeProcessingJob.update_forward_refs()