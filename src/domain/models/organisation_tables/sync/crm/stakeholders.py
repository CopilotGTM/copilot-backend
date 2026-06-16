from enum import Enum


class StakeholderStance(str, Enum):
    UNKNOWN = "UNKNOWN"
    CHAMPION = "CHAMPION"
    SUPPORTER = "SUPPORTER"
    NEUTRAL = "NEUTRAL"
    DETRACTOR = "DETRACTOR"
    BLOCKER = "BLOCKER"


class DecisionPower(str, Enum):
    UNKNOWN = "UNKNOWN"
    DECISION_MAKER = "DECISION_MAKER"
    INFLUENCER = "INFLUENCER"
    EVALUATOR = "EVALUATOR"
    GATEKEEPER = "GATEKEEPER"
    END_USER = "END_USER"


class StakeholderTemperature(str, Enum):
    """Temperature indicating engagement level with stakeholder.

    TODO: Existing records may have COLD as old default. Future engagement
    analysis tasks should properly assess and update temperatures. Do not
    bulk-update existing COLD records to UNKNOWN - let analysis determine
    the correct value based on actual engagement data.
    """

    UNKNOWN = "UNKNOWN"
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


class StakeholderSource(str, Enum):
    CRM = "CRM"
    CALENDAR_DISCOVERY = "CALENDAR_DISCOVERY"
    EMAIL_DISCOVERY = "EMAIL_DISCOVERY"
    MANUAL = "MANUAL"


class ApprovalStatus(str, Enum):
    """Approval status for stakeholders.

    - CRM stakeholders are always APPROVED (trusted source)
    - CALENDAR_DISCOVERY: First 5 per deal auto-APPROVED, rest PENDING
    - EMAIL_DISCOVERY: First 5 per deal auto-APPROVED, rest PENDING
    - User can REJECT any stakeholder to exclude from matching
    - User can REMOVE an APPROVED stakeholder from the deal
    """

    APPROVED = "APPROVED"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    REMOVED = "REMOVED"
