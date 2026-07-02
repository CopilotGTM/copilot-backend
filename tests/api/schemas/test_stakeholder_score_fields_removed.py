from __future__ import annotations

from src.agents.tools.crm.create_stakeholder_if_missing import CreateStakeholderIfMissingInput
from src.agents.workflows.stakeholder_analysis.models import StakeholderAnalysisData
from src.api.schemas import crm_models
from src.contracts.crm import stakeholders as stakeholder_contracts


def test_api_stakeholder_response_models_do_not_expose_removed_scores() -> None:
    removed_fields = {"champion_potential", "engagement_score"}

    assert removed_fields.isdisjoint(crm_models.StakeholderDetail.model_fields)
    assert removed_fields.isdisjoint(crm_models.StakeholderFullDetail.model_fields)
    assert removed_fields.isdisjoint(crm_models.StakeholderEngagementSnapshot.model_fields)


def test_api_stakeholder_write_models_do_not_accept_removed_scores() -> None:
    assert "champion_potential" not in crm_models.CreateStakeholderRequest.model_fields
    assert "champion_potential" not in crm_models.UpdateStakeholderRequest.model_fields


def test_shared_stakeholder_contracts_do_not_expose_removed_scores() -> None:
    removed_fields = {"champion_potential", "engagement_score"}

    assert removed_fields.isdisjoint(stakeholder_contracts.StakeholderDetail.model_fields)
    assert "champion_potential" not in stakeholder_contracts.CreateStakeholderCommand.model_fields
    assert "champion_potential" not in stakeholder_contracts.UpdateStakeholderCommand.model_fields


def test_agent_stakeholder_write_models_do_not_expose_removed_scores() -> None:
    removed_fields = {"champion_potential", "engagement_score"}

    assert removed_fields.isdisjoint(StakeholderAnalysisData.model_fields)
    assert removed_fields.isdisjoint(CreateStakeholderIfMissingInput.model_fields)
