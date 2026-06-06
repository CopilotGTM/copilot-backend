from .activities import (
    CRMActivity,
    CRMActivityObjectType,
    CRMActivityPriority,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
    # Backwards compatibility aliases
    CRMDealTask,
    CRMTaskPriority,
    CRMTaskStatus,
    CRMTaskType,
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
    DealStakeholder,
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
    "DealStakeholder",
    "StakeholderStance",
    "DecisionPower",
    "StakeholderTemperature",
    "StakeholderSource",
]
