"""Public import surface for organisation-scoped CRM models.

Keeping model exports centralized lets services and migrations import stable
names while individual CRM model files continue to own table definitions.
"""

from .activities import (
    CRMActivity,
    CRMActivityEmailDisplayPayload,
    CRMActivityLink,
    CRMActivityMeetingDisplayPayload,
    CRMActivityObjectType,
    CRMActivityParticipantPreview,
    CRMActivityPriority,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
    # Backwards compatibility aliases
    CRMDealTask,
    CRMNote,
    CRMTask,
    CRMTaskPriority,
    CRMTaskStatus,
    CRMTaskType,
    validate_crm_activity_display_payload,
)
from .associations import CRMAssociationCategory, CRMObjectAssociation, CRMObjectType
from .companies import CRMCompany
from .contacts import CRMContact
from .custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from .deals import (
    CRMDeal,
    CRMDealMutationEvent,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMParticipantType,
    CRMPipeline,
    CRMStageState,
)
from .lead_forms import (
    LeadForm,
    LeadFormField,
    LeadFormFieldDataType,
    LeadFormFieldTargetType,
    LeadFormStatus,
    LeadFormSubmission,
)
from .leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadParticipantType,
    CRMLeadPriority,
    CRMLeadSourceType,
    CRMLeadStatus,
    CRMLeadStatusHistory,
)
from .source_mappings import CRMSourceKind, CRMSourceObjectMapping, CRMSourceObjectType
from .stakeholders import (
    ApprovalStatus,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from .users import CRMUser

__all__ = [
    # Activities (generalized from Tasks)
    "CRMActivity",
    "CRMActivityStatus",
    "CRMActivityType",
    "CRMActivityPriority",
    "CRMActivityObjectType",
    "CRMActivitySource",
    "CRMActivityParticipantPreview",
    "CRMActivityEmailDisplayPayload",
    "CRMActivityMeetingDisplayPayload",
    "validate_crm_activity_display_payload",
    "CRMActivityLink",
    "CRMTask",
    "CRMNote",
    # Backwards compatibility aliases for Tasks
    "CRMDealTask",
    "CRMTaskStatus",
    "CRMTaskType",
    "CRMTaskPriority",
    # Associations
    "CRMObjectAssociation",
    "CRMObjectType",
    "CRMAssociationCategory",
    # Contacts
    "CRMContact",
    # Custom Field Definitions
    "CRMCustomFieldDefinition",
    "CRMCustomFieldEntity",
    "CRMCustomFieldDataType",
    "CRMCustomFieldSource",
    # Pipelines and Stages
    "CRMPipeline",
    "CRMDealStage",
    "CRMStageState",
    # Deals
    "CRMDeal",
    "CRMDealMutationEvent",
    "CRMDealParticipant",
    "CRMDealStageHistory",
    "CRMDealStatus",
    "CRMParticipantType",
    # Leads
    "CRMLead",
    "CRMLeadParticipant",
    "CRMLeadParticipantType",
    "CRMLeadPriority",
    "CRMLeadSourceType",
    "CRMLeadStatus",
    "CRMLeadStatusHistory",
    "LeadForm",
    "LeadFormField",
    "LeadFormFieldDataType",
    "LeadFormFieldTargetType",
    "LeadFormStatus",
    "LeadFormSubmission",
    # Source mappings
    "CRMSourceObjectMapping",
    "CRMSourceKind",
    "CRMSourceObjectType",
    # Companies
    "CRMCompany",
    # Users
    "CRMUser",
    # Stakeholders
    "ApprovalStatus",
    "StakeholderStance",
    "DecisionPower",
    "StakeholderTemperature",
    "StakeholderSource",
]
