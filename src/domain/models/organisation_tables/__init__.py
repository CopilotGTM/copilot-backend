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
from .chat_message_models import Attachment, ChatMessage, ChatMessageAttachmentLink
from .chat_session_models import ChatSession
from .job_models import (
    ChatMessageJob,
    DealJobs,
    DealJobType,
    JobStatus,
    KnowledgeProcessingJob,
)
from .knowledge_models import OrganisationDocument, OrganisationSource
from .reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
    UserReminder,
)
from .sync import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CRMAssociationCategory,
    CRMCompany,
    CRMContact,
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMDealTask,
    CRMObjectAssociation,
    CRMObjectType,
    CRMParticipantType,
    CRMPipeline,
    CRMStageState,
    CRMUser,
    DealStakeholder,
    IntegrationScope,
    IntegrationSyncHistory,
    IntegrationType,
    KnowledgeIntegration,
    SyncCategory,
    SyncedRecordMixin,
    SyncStatus,
)

__all__ = [
    # Base Mixins
    "TimestampMixin",
    "CreatedAtMixin",
    # Chat Models
    "ChatSession",
    "ChatMessage",
    "Attachment",
    "ChatMessageAttachmentLink",
    # Knowledge Models
    "OrganisationDocument",
    "OrganisationSource",
    "KnowledgeIntegration",
    "IntegrationSyncHistory",
    "IntegrationType",
    "IntegrationScope",
    "SyncStatus",
    # Synced Data
    "SyncCategory",
    "SyncedRecordMixin",
    "CalendarEvent",
    "CalendarAttendee",
    "CalendarEventDealAssociation",
    "CRMContact",
    "CRMCompany",
    "CRMUser",
    "CRMObjectAssociation",
    "CRMObjectType",
    "CRMAssociationCategory",
    "CRMCustomFieldDefinition",
    "CRMCustomFieldEntity",
    "CRMCustomFieldDataType",
    "CRMPipeline",
    "CRMDealStage",
    "CRMDeal",
    "CRMDealParticipant",
    "CRMDealStageHistory",
    "CRMDealTask",
    "CRMParticipantType",
    "CRMDealStatus",
    "CRMStageState",
    "DealStakeholder",
    # Job Models
    "JobStatus",
    "ChatMessageJob",
    "KnowledgeProcessingJob",
    "DealJobs",
    "DealJobType",
    # Reminder Models
    "UserReminder",
    "ReminderType",
    "ReminderRecurrence",
    "ReminderStatus",
]
