"""Regression tests for dashboard stage movement analytics calculations."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.service.crm.analytics.stage_movement_service import (
    StageMovementDeal,
    StageMovementHistory,
    calculate_stage_movement_analytics,
)


def test_stage_movement_analytics_returns_empty_metrics_without_data() -> None:
    """No history/deals should return empty summary and no transition rows."""
    result = calculate_stage_movement_analytics(
        deals=[],
        history=[],
        from_date=datetime(2026, 4, 1, tzinfo=UTC),
        to_date=datetime(2026, 5, 1, tzinfo=UTC),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert result.summary.moved_deals == 0
    assert result.summary.moved_from_qualified == 0
    assert result.summary.average_transition_days is None
    assert result.summary.stuck_deals == 0
    assert result.stage_transitions == []
    assert result.qualified_progression == []
    assert result.stuck_stages == []
    assert result.recent_movements == []


def test_stage_movement_analytics_ignores_initial_stage_captures() -> None:
    """First-stage captures without prior stage are excluded from movement counts."""
    deal_id = uuid4()
    period_start = datetime(2026, 4, 1, tzinfo=UTC)
    period_end = datetime(2026, 5, 1, tzinfo=UTC)

    result = calculate_stage_movement_analytics(
        deals=[
            StageMovementDeal(
                deal_id=deal_id,
                name="Imported Deal",
                amount=100000,
                status="open",
                stage="Proposal",
                stage_entered_at=datetime(2026, 4, 10, tzinfo=UTC),
            )
        ],
        history=[
            StageMovementHistory(
                deal_id=deal_id,
                deal_name="Imported Deal",
                amount=100000,
                owner_name="Nikhil",
                from_stage=None,
                to_stage="Proposal",
                entered_at=datetime(2026, 4, 10, tzinfo=UTC),
                previous_entered_at=None,
            )
        ],
        from_date=period_start,
        to_date=period_end,
        now=period_end,
    )

    assert result.summary.moved_deals == 0
    assert result.summary.moved_from_qualified == 0
    assert result.summary.average_transition_days is None
    assert result.stage_transitions == []
    assert result.qualified_progression == []
    assert result.recent_movements == []


def test_stage_movement_analytics_groups_transitions_and_qualified_progression() -> None:
    """Aggregate transitions by edge and by qualified-start progression."""
    deal_one = uuid4()
    deal_two = uuid4()
    period_start = datetime(2026, 4, 1, tzinfo=UTC)
    period_end = datetime(2026, 5, 1, tzinfo=UTC)

    result = calculate_stage_movement_analytics(
        deals=[
            StageMovementDeal(
                deal_id=deal_one,
                name="Qualified Deal",
                amount=100000,
                status="open",
                stage="Proposal",
                stage_entered_at=datetime(2026, 4, 10, tzinfo=UTC),
            ),
            StageMovementDeal(
                deal_id=deal_two,
                name="Negotiation Deal",
                amount=200000,
                status="open",
                stage="Negotiation",
                stage_entered_at=datetime(2026, 4, 12, tzinfo=UTC),
            ),
        ],
        history=[
            StageMovementHistory(
                deal_id=deal_one,
                deal_name="Qualified Deal",
                amount=100000,
                owner_name="Nikhil",
                from_stage="Qualification",
                to_stage="Proposal",
                entered_at=datetime(2026, 4, 10, tzinfo=UTC),
                previous_entered_at=datetime(2026, 4, 1, tzinfo=UTC),
            ),
            StageMovementHistory(
                deal_id=deal_two,
                deal_name="Negotiation Deal",
                amount=200000,
                owner_name="Saksham",
                from_stage="Proposal",
                to_stage="Negotiation",
                entered_at=datetime(2026, 4, 20, tzinfo=UTC),
                previous_entered_at=datetime(2026, 4, 12, tzinfo=UTC),
            ),
        ],
        from_date=period_start,
        to_date=period_end,
        now=period_end,
    )

    assert result.summary.moved_deals == 2
    assert result.summary.moved_from_qualified == 1
    assert result.summary.average_transition_days == 8.5

    assert len(result.stage_transitions) == 2
    qualified_transition = next(
        row
        for row in result.stage_transitions
        if row.from_stage == "Qualification" and row.to_stage == "Proposal"
    )
    assert qualified_transition.deal_count == 1
    assert qualified_transition.total_value == 100000
    assert qualified_transition.average_transition_days == 9

    assert len(result.qualified_progression) == 1
    assert result.qualified_progression[0].to_stage == "Proposal"
    assert result.qualified_progression[0].deal_count == 1
    assert result.qualified_progression[0].total_value == 100000


def test_stage_movement_analytics_treats_qualified_stage_variants_as_progression() -> None:
    """Treat qualified stage variants as qualified-source for progression metrics."""
    deal_id = uuid4()
    period_start = datetime(2026, 4, 1, tzinfo=UTC)
    period_end = datetime(2026, 5, 1, tzinfo=UTC)

    result = calculate_stage_movement_analytics(
        deals=[],
        history=[
            StageMovementHistory(
                deal_id=deal_id,
                deal_name="Qualified To Buy Deal",
                amount=75000,
                owner_name="Nikhil",
                from_stage="Qualified To Buy",
                to_stage="Closed Won",
                entered_at=datetime(2026, 4, 20, tzinfo=UTC),
                previous_entered_at=datetime(2026, 4, 10, tzinfo=UTC),
            )
        ],
        from_date=period_start,
        to_date=period_end,
        now=period_end,
    )

    assert result.summary.moved_from_qualified == 1
    assert len(result.qualified_progression) == 1
    assert result.qualified_progression[0].to_stage == "Closed Won"
    assert result.qualified_progression[0].deal_count == 1
    assert result.qualified_progression[0].total_value == 75000


def test_stage_movement_analytics_counts_only_open_stuck_deals() -> None:
    """Only open deals beyond threshold should appear in stuck-stage summaries."""
    now = datetime(2026, 5, 1, tzinfo=UTC)
    stale_entered_at = now - timedelta(days=45)

    result = calculate_stage_movement_analytics(
        deals=[
            StageMovementDeal(
                deal_id=uuid4(),
                name="Open Stuck",
                amount=50000,
                status="open",
                stage="Proposal",
                stage_entered_at=stale_entered_at,
            ),
            StageMovementDeal(
                deal_id=uuid4(),
                name="Won Old",
                amount=100000,
                status="won",
                stage="Won",
                stage_entered_at=stale_entered_at,
            ),
            StageMovementDeal(
                deal_id=uuid4(),
                name="Fresh Open",
                amount=25000,
                status="open",
                stage="Proposal",
                stage_entered_at=now - timedelta(days=5),
            ),
        ],
        history=[],
        from_date=None,
        to_date=None,
        now=now,
        stale_threshold_days=30,
    )

    assert result.summary.stuck_deals == 1
    assert len(result.stuck_stages) == 1
    assert result.stuck_stages[0].stage == "Proposal"
    assert result.stuck_stages[0].stuck_deals == 1
    assert result.stuck_stages[0].average_days_in_stage == 45
    assert result.stuck_stages[0].total_value == 50000
