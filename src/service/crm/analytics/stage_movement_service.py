"""Core stage-movement analytics used for dashboard KPIs.

The calculators here are pure functions over already-fetched deal and history
rows, which keeps them safe to unit-test and avoids DB session or model-layer
coupling during dashboard reads.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class StageMovementDeal:
    """Deal snapshot consumed by analytics for stuck-stage and transition reporting."""

    deal_id: UUID
    name: str | None
    amount: float | None
    status: str | None
    stage: str | None
    stage_entered_at: datetime | None


@dataclass(frozen=True)
class StageMovementHistory:
    """One captured stage transition event used to derive movement and aging KPIs."""

    deal_id: UUID
    deal_name: str | None
    amount: float | None
    owner_name: str | None
    from_stage: str | None
    to_stage: str | None
    entered_at: datetime
    previous_entered_at: datetime | None


@dataclass(frozen=True)
class StageMovementSummary:
    """Top-line movement counters and latency for the current dashboard window."""

    moved_deals: int
    moved_from_qualified: int
    average_transition_days: float | None
    stuck_deals: int
    stale_threshold_days: int


@dataclass(frozen=True)
class StageTransitionAnalytics:
    """Aggregate counts/value/duration grouped by a stage transition edge."""

    from_stage: str | None
    to_stage: str
    deal_count: int
    total_value: float
    average_transition_days: float | None


@dataclass(frozen=True)
class QualifiedProgressionAnalytics:
    """Aggregate movement metrics for transitions that start from qualified stage names."""

    to_stage: str
    deal_count: int
    total_value: float
    average_transition_days: float | None


@dataclass(frozen=True)
class StuckStageAnalytics:
    """Bucket of deals that have remained in an open stage beyond threshold."""

    stage: str
    stuck_deals: int
    average_days_in_stage: float
    max_days_in_stage: int
    total_value: float


@dataclass(frozen=True)
class RecentStageMovement:
    """Lightweight projection of the latest qualifying stage transitions."""

    deal_id: UUID
    deal_name: str | None
    from_stage: str | None
    to_stage: str | None
    changed_at: datetime
    owner_name: str | None
    value: float | None


@dataclass(frozen=True)
class StageMovementAnalytics:
    """Full response payload returned by calculate_stage_movement_analytics."""

    summary: StageMovementSummary
    qualified_progression: list[QualifiedProgressionAnalytics]
    stage_transitions: list[StageTransitionAnalytics]
    stuck_stages: list[StuckStageAnalytics]
    recent_movements: list[RecentStageMovement]


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _stage_label(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _is_qualified_stage(value: str | None) -> bool:
    label = _stage_label(value)
    if label is None:
        return False
    normalized = label.casefold()
    return normalized in {"qualified", "qualification"} or normalized.startswith("qualified ")


def _is_open_status(value: str | None) -> bool:
    if value is None:
        return True
    return value.casefold() == "open"


def _is_stage_transition(history: StageMovementHistory) -> bool:
    from_stage = _stage_label(history.from_stage)
    to_stage = _stage_label(history.to_stage)
    if from_stage is None or to_stage is None:
        return False
    return from_stage.casefold() != to_stage.casefold()


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    average = sum(values) / len(values)
    return round(average, 2) if average % 1 else int(average)


def _transition_days(history: StageMovementHistory) -> float | None:
    previous_entered_at = _as_aware(history.previous_entered_at)
    entered_at = _as_aware(history.entered_at)
    if previous_entered_at is None or entered_at is None:
        return None
    if entered_at < previous_entered_at:
        return None
    return (entered_at - previous_entered_at).total_seconds() / 86400


def _in_period(
    history: StageMovementHistory,
    *,
    from_date: datetime | None,
    to_date: datetime | None,
) -> bool:
    entered_at = _as_aware(history.entered_at)
    if entered_at is None:
        return False
    start = _as_aware(from_date)
    end = _as_aware(to_date)
    if start is not None and entered_at < start:
        return False
    return not (end is not None and entered_at > end)


def calculate_stage_movement_analytics(
    *,
    deals: list[StageMovementDeal],
    history: list[StageMovementHistory],
    from_date: datetime | None,
    to_date: datetime | None,
    now: datetime | None = None,
    stale_threshold_days: int = 30,
    recent_limit: int = 10,
) -> StageMovementAnalytics:
    """Build movement KPIs for the selected period from denormalized history input.

    The function separates transition counting, qualified-stage progression, stuck
    deal detection, and recent movement projection so each KPI can evolve
    independently without duplicating the same scan logic.
    """
    current_time = _as_aware(now) or datetime.now(UTC)
    period_history = [
        item
        for item in history
        if _is_stage_transition(item) and _in_period(item, from_date=from_date, to_date=to_date)
    ]

    transition_groups: dict[tuple[str | None, str], dict[str, Any]] = defaultdict(
        lambda: {"deal_ids": set(), "total_value": 0.0, "durations": []}
    )
    qualified_groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"deal_ids": set(), "total_value": 0.0, "durations": []}
    )
    moved_deal_ids: set[UUID] = set()
    moved_from_qualified_ids: set[UUID] = set()
    all_transition_days: list[float] = []

    for item in period_history:
        to_stage = _stage_label(item.to_stage)
        if to_stage is None:
            continue
        from_stage = _stage_label(item.from_stage)
        value = float(item.amount or 0)
        duration = _transition_days(item)

        moved_deal_ids.add(item.deal_id)
        group = transition_groups[(from_stage, to_stage)]
        group["deal_ids"].add(item.deal_id)
        group["total_value"] += value
        if duration is not None:
            group["durations"].append(duration)
            all_transition_days.append(duration)

        if _is_qualified_stage(from_stage):
            moved_from_qualified_ids.add(item.deal_id)
            qualified_group = qualified_groups[to_stage]
            qualified_group["deal_ids"].add(item.deal_id)
            qualified_group["total_value"] += value
            if duration is not None:
                qualified_group["durations"].append(duration)

    stage_transitions = [
        StageTransitionAnalytics(
            from_stage=from_stage,
            to_stage=to_stage,
            deal_count=len(data["deal_ids"]),
            total_value=data["total_value"],
            average_transition_days=_average(data["durations"]),
        )
        for (from_stage, to_stage), data in transition_groups.items()
    ]
    stage_transitions.sort(key=lambda row: (-row.deal_count, -row.total_value, row.to_stage))

    qualified_progression = [
        QualifiedProgressionAnalytics(
            to_stage=to_stage,
            deal_count=len(data["deal_ids"]),
            total_value=data["total_value"],
            average_transition_days=_average(data["durations"]),
        )
        for to_stage, data in qualified_groups.items()
    ]
    qualified_progression.sort(key=lambda row: (-row.deal_count, -row.total_value, row.to_stage))

    stuck_groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "total_value": 0.0, "stage_days": []}
    )
    for deal in deals:
        if not _is_open_status(deal.status):
            continue
        entered_at = _as_aware(deal.stage_entered_at)
        stage = _stage_label(deal.stage)
        if entered_at is None or stage is None:
            continue
        days_in_stage = (current_time - entered_at).days
        if days_in_stage < stale_threshold_days:
            continue
        group = stuck_groups[stage]
        group["count"] += 1
        group["total_value"] += float(deal.amount or 0)
        group["stage_days"].append(days_in_stage)

    stuck_stages = [
        StuckStageAnalytics(
            stage=stage,
            stuck_deals=data["count"],
            average_days_in_stage=_average(data["stage_days"]) or 0,
            max_days_in_stage=max(data["stage_days"]),
            total_value=data["total_value"],
        )
        for stage, data in stuck_groups.items()
    ]
    stuck_stages.sort(key=lambda row: (-row.stuck_deals, -row.total_value, row.stage))

    recent_movements = [
        RecentStageMovement(
            deal_id=item.deal_id,
            deal_name=item.deal_name,
            from_stage=_stage_label(item.from_stage),
            to_stage=_stage_label(item.to_stage),
            changed_at=item.entered_at,
            owner_name=item.owner_name,
            value=item.amount,
        )
        for item in sorted(period_history, key=lambda row: row.entered_at, reverse=True)[
            :recent_limit
        ]
    ]

    summary = StageMovementSummary(
        moved_deals=len(moved_deal_ids),
        moved_from_qualified=len(moved_from_qualified_ids),
        average_transition_days=_average(all_transition_days),
        stuck_deals=sum(row.stuck_deals for row in stuck_stages),
        stale_threshold_days=stale_threshold_days,
    )

    return StageMovementAnalytics(
        summary=summary,
        qualified_progression=qualified_progression,
        stage_transitions=stage_transitions,
        stuck_stages=stuck_stages,
        recent_movements=recent_movements,
    )
