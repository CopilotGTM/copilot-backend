from .activities import (
    CRMActivity,
    CRMActivityObjectType,
    CRMActivityPriority,
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
from .stakeholders import (
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
    # Companies
    "CRMCompany",
    # Users
    "CRMUser",
    # Stakeholders
    "DealStakeholder",
    "StakeholderStance",
    "DecisionPower",
    "StakeholderTemperature",
    "StakeholderSource",
]
