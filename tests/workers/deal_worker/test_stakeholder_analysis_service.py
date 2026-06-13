from types import SimpleNamespace
from uuid import uuid4

from src.agents.workflows.stakeholder_analysis.models import StakeholderAnalysisData
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DecisionPower,
    StakeholderStance,
    StakeholderTemperature,
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


def test_update_stakeholder_with_analysis_accepts_participant_rows() -> None:
    service = StakeholderAnalysisService(organisation_id=uuid4())
    participant = SimpleNamespace(
        participant_id=uuid4(),
        deal_id=uuid4(),
        related_email="buyer@example.com",
        stance=StakeholderStance.UNKNOWN.value,
        decision_power=DecisionPower.UNKNOWN.value,
        temperature=StakeholderTemperature.UNKNOWN.value,
        motivation=None,
        pain_points=None,
        keywords=None,
    )
    analysis = StakeholderAnalysisData(
        stakeholder_email="buyer@example.com",
        stance=StakeholderStance.CHAMPION,
        decision_power=DecisionPower.DECISION_MAKER,
        temperature=StakeholderTemperature.HOT,
        motivation="Wants faster close visibility",
        pain_points=["manual reporting", "slow handoffs"],
        keywords=["forecast", "approval"],
        analysis_summary="Strong champion with final authority.",
    )

    service.update_stakeholder_with_analysis(participant, analysis)

    assert participant.stance == StakeholderStance.CHAMPION.value
    assert participant.decision_power == DecisionPower.DECISION_MAKER.value
    assert participant.temperature == StakeholderTemperature.HOT.value
    assert participant.motivation == "Wants faster close visibility"
    assert participant.pain_points == ["manual reporting", "slow handoffs"]
    assert participant.keywords == ["forecast", "approval"]
    assert not hasattr(participant, "stakeholder_engagement_history")
