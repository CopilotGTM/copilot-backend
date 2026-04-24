"""Salesforce OpportunityHistory -> NormalizedSalesforceStageTransition."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    NormalizedSalesforceStageTransition,
)

logger = get_logger(__name__)


def normalize_stage_history(
    raw_rows: Iterable[dict[str, Any]],
    *,
    pipeline_external_id_by_deal: Mapping[str, str],
) -> tuple[list[NormalizedSalesforceStageTransition], int]:
    grouped: dict[str, list[_StageRow]] = defaultdict(list)
    skipped_malformed = 0

    for raw in raw_rows:
        row = _normalize_stage_row(raw, pipeline_external_id_by_deal)
        if row is None:
            skipped_malformed += 1
            continue
        grouped[row.deal_external_id].append(row)

    out: list[NormalizedSalesforceStageTransition] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for deal_external_id, rows in grouped.items():
        rows.sort(key=lambda item: item.entered_at)
        for index, row in enumerate(rows):
            previous = rows[index - 1] if index > 0 else None
            next_row = rows[index + 1] if index + 1 < len(rows) else None
            key = (
                deal_external_id,
                row.stage_external_id,
                row.entered_at.isoformat(),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(
                NormalizedSalesforceStageTransition(
                    deal_external_id=deal_external_id,
                    stage_external_id=row.stage_external_id,
                    from_stage_external_id=(
                        previous.stage_external_id if previous is not None else None
                    ),
                    entered_at=row.entered_at,
                    exited_at=(next_row.entered_at if next_row is not None else None),
                    elapsed_seconds=(
                        int((next_row.entered_at - row.entered_at).total_seconds())
                        if next_row is not None
                        else None
                    ),
                    amount_at_stage=row.amount_at_stage,
                    probability_at_stage=row.probability_at_stage,
                    source=None,
                    data_payload=row.data_payload,
                )
            )
    return out, skipped_malformed


def current_stage_entered_at_by_deal(
    transitions: Iterable[NormalizedSalesforceStageTransition],
    *,
    current_stage_external_id_by_deal: Mapping[str, str | None],
) -> dict[str, datetime]:
    latest_by_deal: dict[str, NormalizedSalesforceStageTransition] = {}
    for transition in transitions:
        existing = latest_by_deal.get(transition.deal_external_id)
        if existing is None or transition.entered_at > existing.entered_at:
            latest_by_deal[transition.deal_external_id] = transition

    out: dict[str, datetime] = {}
    for deal_external_id, transition in latest_by_deal.items():
        if current_stage_external_id_by_deal.get(deal_external_id) != transition.stage_external_id:
            continue
        out[deal_external_id] = transition.entered_at
    return out


class _StageRow:
    def __init__(
        self,
        *,
        deal_external_id: str,
        stage_external_id: str,
        entered_at: datetime,
        amount_at_stage: float | None,
        probability_at_stage: float | None,
        data_payload: dict[str, Any],
    ) -> None:
        self.deal_external_id = deal_external_id
        self.stage_external_id = stage_external_id
        self.entered_at = entered_at
        self.amount_at_stage = amount_at_stage
        self.probability_at_stage = probability_at_stage
        self.data_payload = data_payload


def _normalize_stage_row(
    raw: object,
    pipeline_external_id_by_deal: Mapping[str, str],
) -> _StageRow | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict Salesforce OpportunityHistory payload")
        return None

    deal_external_id = _coerce_str(raw.get("OpportunityId"))
    stage_name = _coerce_str(raw.get("StageName"))
    entered_at = _parse_salesforce_timestamp(raw.get("CreatedDate"))
    if deal_external_id is None or stage_name is None or entered_at is None:
        logger.warning(
            "Skipping malformed Salesforce OpportunityHistory row",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    pipeline_external_id = pipeline_external_id_by_deal.get(deal_external_id, "master")
    return _StageRow(
        deal_external_id=deal_external_id,
        stage_external_id=f"{pipeline_external_id}:{stage_name}",
        entered_at=entered_at,
        amount_at_stage=_coerce_float(raw.get("Amount")),
        probability_at_stage=_coerce_probability(raw.get("Probability")),
        data_payload=raw,
    )


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _coerce_probability(value: Any) -> float | None:
    as_float = _coerce_float(value)
    if as_float is None:
        return None
    if as_float < 0.0:
        return 0.0
    if as_float > 100.0:
        return 100.0
    return as_float


def _parse_salesforce_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value
    if len(cleaned) >= 5 and cleaned[-5] in {"+", "-"} and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None
