"""
Organisation DB Tables
====================

This package contains the SQLModel definitions for the organisation-specific database,
which is responsible for storing data related to chat sessions, messages, and knowledge base embeddings.

The models are organised into separate files based on their domain:
- `chat_session_models`: Manages chat sessions.
- `chat_message_models`: Manages chat messages and attachments.
- `knowledge_models`: Manages knowledge base documents and their embeddings.
- `job_models`: Manages background jobs for chat messages and knowledge processing.
- `base_mixins`: Provides common mixin classes for timestamps.

Each model is a SQLModel class that also serves as a Pydantic model for API validation.
"""

from .base_mixins import CreatedAtMixin, TimestampMixin
from .chat_session_models import ChatSession
from .chat_message_models import ChatMessage, Attachment, ChatMessageAttachmentLink
from .knowledge_models import (
    OrganisationDocument,
    OrganisationSource,
    KnowledgeIntegration,
)
from .job_models import JobStatus, ChatMessageJob, KnowledgeProcessingJob

__all__ = [
    # Base Mixins
    "TimestampMixin",
    "CreatedAtMixin",
    # Chat Models
    "ChatSession",
    "ChatMessage",
    "Attachment",
    # Knowledge Models
    "OrganisationDocument",
    "OrganisationSource",
    "KnowledgeIntegration",
    # Job Models
    "JobStatus",
    "ChatMessageJob",
    "KnowledgeProcessingJob",
]
