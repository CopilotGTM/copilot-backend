"""Synced data domain models (calendar + future CRM, email, etc.)."""

from .base import SyncCategory, SyncedRecordMixin
from .calendar import CalendarAttendee, CalendarEvent
from .crm import (
    CRMContact,
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMParticipantType,
    CRMPipeline,
    CRMStageState,
)
from .integration_models import (
    IntegrationScope,
    IntegrationSyncHistory,
    IntegrationType,
    KnowledgeIntegration,
    SyncStatus,
)

__all__ = [
    "SyncCategory",
    "SyncedRecordMixin",
    "CalendarEvent",
    "CalendarAttendee",
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
    "CRMParticipantType",
    "CRMDealStatus",
    "CRMStageState",
]
