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
The sync exports also include meeting-recording session state used by the Recall
meeting bot flow.
"""

from .base_mixins import CreatedAtMixin, TimestampMixin
from .chat_message_models import Attachment, ChatMessage, ChatMessageAttachmentLink
from .chat_session_models import ChatSession
from .deal_asset_models import (
    DealAsset,
    DealAssetDirection,
    DealAssetLinkStatus,
    DealAssetSourceType,
    DealAssetStatus,
)
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
    ReminderRunStatus,
    ReminderStatus,
    ReminderType,
    UserReminder,
    UserReminderRun,
)
from .sync import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
    CRMAssociationCategory,
    CRMCompany,
    CRMContact,
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
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
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
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
    "CalendarEventUserState",
    "MeetingRecordingProvider",
    "MeetingRecordingStatus",
    "MeetingTranscriptStatus",
    "MeetingRecordingSession",
    "CRMContact",
    "CRMCompany",
    "CRMUser",
    "CRMObjectAssociation",
    "CRMObjectType",
    "CRMAssociationCategory",
    "CRMCustomFieldDefinition",
    "CRMCustomFieldEntity",
    "CRMCustomFieldDataType",
    "CRMCustomFieldSource",
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
    # Deal Asset Models
    "DealAsset",
    "DealAssetStatus",
    "DealAssetDirection",
    "DealAssetSourceType",
    "DealAssetLinkStatus",
    # Job Models
    "JobStatus",
    "ChatMessageJob",
    "KnowledgeProcessingJob",
    "DealJobs",
    "DealJobType",
    # Reminder Models
    "UserReminder",
    "UserReminderRun",
    "ReminderType",
    "ReminderRecurrence",
    "ReminderStatus",
    "ReminderRunStatus",
]
