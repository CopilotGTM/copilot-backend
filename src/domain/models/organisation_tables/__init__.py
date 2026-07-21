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

# Keep every org-scoped model export in one place so callers can import a stable
# package-level surface for both legacy SQLModel usage and type-checking.

from .agent_models import (
    AgentOutputMode,
    AgentRun,
    AgentRunStatus,
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
    AutomationScopeType,
    AutomationTargetType,
    AutomationTrigger,
    AutomationTriggerStatus,
    AutomationTriggerType,
    UserAgent,
)
from .base_mixins import CreatedAtMixin, TimestampMixin
from .chat_message_models import Attachment, ChatMessage, ChatMessageAttachmentLink
from .chat_session_access_models import ChatSessionAccess
from .chat_session_models import ChatSession
from .dashboard_models import DashboardSnapshot
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
    CRMImportRow,
    CRMImportRowStatus,
    CRMLead,
    CRMLeadParticipant,
    CRMLeadParticipantType,
    CRMLeadPriority,
    CRMLeadSourceType,
    CRMLeadStatus,
    CRMLeadStatusHistory,
    CRMObjectAssociation,
    CRMObjectType,
    CRMParticipantType,
    CRMPipeline,
    CRMSourceKind,
    CRMSourceObjectMapping,
    CRMSourceObjectType,
    CRMStageState,
    CRMUser,
    IntegrationActivityRequest,
    IntegrationActivityRequestStatus,
    IntegrationScope,
    IntegrationSyncHistory,
    IntegrationType,
    KnowledgeIntegration,
    LeadForm,
    LeadFormField,
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
    LeadFormSubmission,
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
    # Agent Automation Models
    "UserAgent",
    "AutomationTrigger",
    "AgentRun",
    "AgentSourceType",
    "AgentVisibility",
    "AgentStatus",
    "AgentOutputMode",
    "AutomationTargetType",
    "AutomationTriggerType",
    "AutomationScopeType",
    "AutomationTriggerStatus",
    "AgentRunStatus",
    # Chat Models
    "ChatSession",
    "ChatSessionAccess",
    "ChatMessage",
    "Attachment",
    "ChatMessageAttachmentLink",
    "DashboardSnapshot",
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
    "IntegrationActivityRequest",
    "IntegrationActivityRequestStatus",
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
    "CRMImportRow",
    "CRMImportRowStatus",
    "CRMLead",
    "CRMLeadParticipant",
    "CRMLeadParticipantType",
    "CRMLeadPriority",
    "CRMLeadSourceType",
    "CRMLeadStatus",
    "CRMLeadStatusHistory",
    "CRMSourceObjectMapping",
    "CRMSourceKind",
    "CRMSourceObjectType",
    "LeadForm",
    "LeadFormField",
    "LeadFormFieldDataType",
    "LeadFormFieldTargetType",
    "LeadFormStatus",
    "LeadFormSubmission",
    "CRMParticipantType",
    "CRMDealStatus",
    "CRMStageState",
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
