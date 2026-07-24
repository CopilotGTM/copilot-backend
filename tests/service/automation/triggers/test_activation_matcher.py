"""Pure matching tests for CRM deal-stage trigger activation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from src.service.automation.triggers.activation import (
    CRMDealStageEvent,
    matches_crm_deal_stage_trigger,
)


def event(**overrides: object) -> CRMDealStageEvent:
    pipeline_id, from_stage_id, to_stage_id = uuid4(), uuid4(), uuid4()
    values = {
        "event_id": uuid4(),
        "deal_id": uuid4(),
        "pipeline_id": pipeline_id,
        "from_stage_id": from_stage_id,
        "to_stage_id": to_stage_id,
        "outcome": "open",
        "occurred_at": datetime(2026, 7, 13, tzinfo=UTC),
        "changed_fields": {"stage_id": {"old": str(from_stage_id), "new": str(to_stage_id)}},
    }
    values.update(overrides)
    return CRMDealStageEvent(**values)  # type: ignore[arg-type]


def filter_config(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_key": "crm.deal.stage_changed.v1",
        "pipeline_ids": [],
        "stage_change_mode": "any_change",
        "from_stage_ids": [],
        "to_stage_ids": [],
        "outcome": "any",
    }
    values.update(overrides)
    return values


def test_any_change_matches_only_selected_pipeline() -> None:
    candidate = event()

    assert matches_crm_deal_stage_trigger(filter_config(), candidate)
    assert matches_crm_deal_stage_trigger(
        filter_config(pipeline_ids=[str(candidate.pipeline_id)]), candidate
    )
    assert not matches_crm_deal_stage_trigger(filter_config(pipeline_ids=[str(uuid4())]), candidate)


def test_outcome_matches_current_won_or_lost_state() -> None:
    won = event(outcome="won")

    assert matches_crm_deal_stage_trigger(
        filter_config(stage_change_mode="outcome", outcome="won"), won
    )
    assert not matches_crm_deal_stage_trigger(
        filter_config(stage_change_mode="outcome", outcome="lost"), won
    )


def test_specific_stage_applies_from_and_to_filters_together() -> None:
    candidate = event()

    assert matches_crm_deal_stage_trigger(
        filter_config(
            stage_change_mode="specific_stage",
            pipeline_ids=[str(candidate.pipeline_id)],
            from_stage_ids=[str(candidate.from_stage_id)],
            to_stage_ids=[str(candidate.to_stage_id)],
        ),
        candidate,
    )
    assert not matches_crm_deal_stage_trigger(
        filter_config(
            stage_change_mode="specific_stage",
            pipeline_ids=[str(candidate.pipeline_id)],
            from_stage_ids=[str(uuid4())],
            to_stage_ids=[str(candidate.to_stage_id)],
        ),
        candidate,
    )
