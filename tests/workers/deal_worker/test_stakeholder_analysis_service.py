from types import SimpleNamespace
from uuid import uuid4

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DecisionPower,
)
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    StakeholderInsightType,
)
from src.workers.deal_worker.services.deal_insight_services.stakeholder_analysis_service import (
    StakeholderAnalysisService,
)


def _make_deal(*, stage_name: str, probability: float = 25.0):
    return SimpleNamespace(
        deal_id=uuid4(),
        stage_label=stage_name,
        stage_name=stage_name,
        forecast_category=None,
        probability=probability,
        deal_status=CRMDealStatus.OPEN,
    )


def _make_stakeholder(
    *,
    decision_power: DecisionPower = DecisionPower.UNKNOWN,
    approval_status: ApprovalStatus = ApprovalStatus.APPROVED,
):
    return SimpleNamespace(
        decision_power=decision_power,
        approval_status=approval_status,
    )


def test_missing_decision_maker_not_flagged_in_discovery_stage() -> None:
    service = StakeholderAnalysisService(organisation_id=uuid4())
    deal = _make_deal(stage_name="Discovery", probability=25.0)
    stakeholders = [_make_stakeholder()]

    insights = service._build_stage_gated_insights(deal, stakeholders)

    assert insights == []


def test_missing_decision_maker_flagged_in_proposal_stage() -> None:
    service = StakeholderAnalysisService(organisation_id=uuid4())
    deal = _make_deal(stage_name="Proposal", probability=50.0)
    stakeholders = [_make_stakeholder()]

    insights = service._build_stage_gated_insights(deal, stakeholders)

    assert len(insights) == 1
    assert insights[0].insight_type == StakeholderInsightType.MISSING_DECISION_MAKER
    assert insights[0].priority == "HIGH"


def test_existing_decision_maker_suppresses_late_stage_warning() -> None:
    service = StakeholderAnalysisService(organisation_id=uuid4())
    deal = _make_deal(stage_name="Negotiation", probability=75.0)
    stakeholders = [
        _make_stakeholder(decision_power=DecisionPower.DECISION_MAKER),
        _make_stakeholder(approval_status=ApprovalStatus.REJECTED),
    ]

    insights = service._build_stage_gated_insights(deal, stakeholders)

    assert insights == []
