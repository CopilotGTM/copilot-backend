"""Synced data domain models (calendar + future CRM, email, etc.)."""

from .base import SyncCategory, SyncedRecordMixin
from .calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)

# DEPRECATED: Use meeting_intelligence models instead for new development
# Kept for backward compatibility and potential future Gong/Chorus sync
from .conversation_intelligence import (
    CICall,
    CICallAnalyticsSnapshot,
    CICallDealLink,
    CICallMedium,
    CICallRecordingStatus,
    CICallSpeaker,
    CICallTranscriptSegment,
    CISpeakerRole,
    TranscriptContentFormat,
)
from .crm import (
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMDealTask,
    CRMParticipantType,
    CRMPipeline,
    CRMStageState,
    CRMUser,
    DealStakeholder,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from .email_models import (
    Email,
    EmailDirection,
    EmailFolder,
    EmailPriority,
    EmailThreadSummary,
)
from .integration_models import (
    IntegrationScope,
    IntegrationSyncHistory,
    IntegrationType,
    KnowledgeIntegration,
    SyncStatus,
)
from .meeting_intelligence import (
    Meeting,
    MeetingSource,
    MeetingStatus,
    TranscriptSegment,
)
from .messaging_models import (
    ChannelType,
    MessageType,
    MessagingChannel,
    MessagingChannelMember,
    MessagingDealLink,
    MessagingMessage,
    MessagingThreadSummary,
)
from .participant_models import IntegrationParticipant, IntegrationParticipantIdentity

__all__ = [
    "SyncCategory",
    "SyncedRecordMixin",
    "CalendarEvent",
    "CalendarAttendee",
    "CalendarEventDealAssociation",
    "AssociationStatus",
    "GenerationStatus",
    "IntegrationType",
    "IntegrationScope",
    "KnowledgeIntegration",
    "IntegrationSyncHistory",
    "SyncStatus",
    "CRMContact",
    "CRMPipeline",
    "CRMDealStage",
    "CRMDeal",
    "CRMDealParticipant",
    "CRMDealStageHistory",
    "CRMDealTask",
    "CRMParticipantType",
    "CRMDealStatus",
    "CRMStageState",
    "CRMUser",
    "CRMCompany",
    "DealStakeholder",
    "StakeholderStance",
    "DecisionPower",
    "StakeholderTemperature",
    "StakeholderSource",
    # Legacy CI models (deprecated - use Meeting* instead)
    "CICall",
    "CICallDealLink",
    "CICallSpeaker",
    "CICallTranscriptSegment",
    "CICallAnalyticsSnapshot",
    "CICallMedium",
    "CICallRecordingStatus",
    "CISpeakerRole",
    "TranscriptContentFormat",
    # New Meeting Intelligence models (minimal - reuses calendar models)
    "MeetingSource",
    "MeetingStatus",
    "Meeting",
    "TranscriptSegment",
    # Participant models
    "IntegrationParticipant",
    "IntegrationParticipantIdentity",
    # Messaging models
    "ChannelType",
    "MessageType",
    "MessagingChannel",
    "MessagingChannelMember",
    "MessagingDealLink",
    "MessagingMessage",
    "MessagingThreadSummary",
    # Email models
    "EmailDirection",
    "EmailPriority",
    "EmailFolder",
    "Email",
    "EmailThreadSummary",
]
