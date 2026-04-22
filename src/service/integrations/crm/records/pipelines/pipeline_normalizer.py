"""HubSpot deal pipelines + stages → internal NormalizedPipeline / NormalizedStage.

Pure functions, no I/O. The HubSpot Pipelines API response is well-defined so
normalization is mostly a coercion layer with three judgement calls:

  1. Probability scaling — HubSpot returns 0.0-1.0 (string), CRMDealStage stores
     0-100 (float).
  2. stage_state derivation — HubSpot only gives `isClosed` + probability;
     CRMStageState splits closed states into WON / LOST.
  3. is_default fallback — older portals omit the `default` field entirely,
     in which case `id == "default"` is the recognised convention.

Invalid input never raises; instead the normalizer signals "skip this row" by
returning None (for stages) or by raising MalformedPipelineError (for whole
pipelines, where the parent has no usable identity).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMStageState
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.pipelines.pipeline_types import (
    NormalizedPipeline,
    NormalizedStage,
)

logger = get_logger(__name__)


class MalformedPipelineError(ValueError):
    """Raised when a HubSpot pipeline payload has no usable `id`.

    Carries the raw payload reference for the caller's WARNING log; the caller
    increments `skipped_pipelines` and continues with the next pipeline.
    """


def normalize_pipelines(
    raw_pipelines: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedPipeline], int, int]:
    """Normalize an iterable of raw HubSpot pipeline dicts.

    Returns (normalized list, skipped_pipelines, skipped_stages). Unparseable
    pipelines are dropped and counted; unparseable stages are dropped and
    counted but their parent pipeline is still emitted.
    """
    pipelines: list[NormalizedPipeline] = []
    skipped_pipelines = 0
    skipped_stages_total = 0
    for raw in raw_pipelines:
        try:
            pipeline, skipped_stages = _normalize_one_pipeline(raw)
        except MalformedPipelineError:
            skipped_pipelines += 1
            logger.warning(
                "Skipping malformed HubSpot pipeline (no usable id)",
                extra={"raw_keys": list(raw.keys()) if isinstance(raw, dict) else None},
            )
            continue
        pipelines.append(pipeline)
        skipped_stages_total += skipped_stages
    return pipelines, skipped_pipelines, skipped_stages_total


def _normalize_one_pipeline(raw: dict[str, Any]) -> tuple[NormalizedPipeline, int]:
    external_id = _coerce_id(raw.get("id"))
    if external_id is None:
        raise MalformedPipelineError("missing id")

    label = _coerce_label(raw.get("label"), fallback=external_id)
    display_order = _coerce_int(raw.get("displayOrder"))
    source_modified_at = _parse_iso(raw.get("updatedAt"))
    is_default = _coerce_default_flag(raw)

    raw_stages = raw.get("stages") or []
    if not isinstance(raw_stages, list):
        raw_stages = []
    stages: list[NormalizedStage] = []
    skipped_stages = 0
    for raw_stage in raw_stages:
        normalized_stage = _normalize_one_stage(raw_stage)
        if normalized_stage is None:
            skipped_stages += 1
            continue
        stages.append(normalized_stage)

    # Strip the inline stages from the pipeline payload — they live as their own
    # rows in CRMDealStage and would otherwise be duplicated in data_payload.
    pipeline_payload = {k: v for k, v in raw.items() if k != "stages"}

    pipeline = NormalizedPipeline(
        external_id=external_id,
        name=label,
        label=label,
        object_type="deals",
        is_default=is_default,
        display_order=display_order,
        source_modified_at=source_modified_at,
        data_payload=pipeline_payload,
        stages=tuple(stages),
    )
    return pipeline, skipped_stages


def _normalize_one_stage(raw: object) -> NormalizedStage | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict HubSpot stage payload")
        return None

    external_id = _coerce_id(raw.get("id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed HubSpot stage (no usable id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    label = _coerce_label(raw.get("label"), fallback=external_id)
    display_order = _coerce_int(raw.get("displayOrder"))
    source_modified_at = _parse_iso(raw.get("updatedAt"))

    metadata_val = raw.get("metadata")
    metadata: dict[str, Any] = metadata_val if isinstance(metadata_val, dict) else {}
    is_closed = _coerce_bool(metadata.get("isClosed"))
    probability_raw = metadata.get("probability")
    probability = _scale_probability(probability_raw)
    stage_state = _derive_stage_state(
        is_closed=is_closed,
        probability=probability,
        external_id=external_id,
        probability_present=probability_raw is not None,
    )

    return NormalizedStage(
        external_id=external_id,
        name=label,
        label=label,
        stage_key=external_id,
        stage_state=stage_state,
        probability=probability,
        is_closed=is_closed,
        display_order=display_order,
        source_modified_at=source_modified_at,
        data_payload=raw,
    )


# ─── coercion helpers ─────────────────────────────────────────────────────────


def _coerce_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int):
        return str(value)
    return None


def _coerce_label(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and value:
        return value
    return fallback


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is a subclass of int — exclude
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _coerce_default_flag(raw: dict[str, Any]) -> bool:
    """`default` field if present; otherwise the legacy `id == "default"` convention."""
    if "default" in raw:
        return _coerce_bool(raw.get("default"))
    return raw.get("id") == "default"


def _scale_probability(value: Any) -> float | None:
    """HubSpot 0.0-1.0 (string or float) → CRMDealStage 0-100 float, bounded.

    Returns None for unparseable input; the caller logs and falls through.
    """
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    scaled = round(as_float * 100, 2)
    if scaled < 0.0:
        return 0.0
    if scaled > 100.0:
        return 100.0
    return scaled


def _derive_stage_state(
    *,
    is_closed: bool,
    probability: float | None,
    external_id: str,
    probability_present: bool,
) -> CRMStageState:
    """Map (isClosed, probability) → CRMStageState.

    Rules:
      isClosed=False                     → OPEN
      isClosed=True, probability == 100  → WON
      isClosed=True, probability < 100   → LOST
      isClosed=True, probability missing → LOST + WARNING (ambiguous source)
    """
    if not is_closed:
        return CRMStageState.OPEN
    if probability is None:
        if probability_present:
            logger.warning(
                "HubSpot stage isClosed=true but probability unparseable; treating as LOST",
                extra={"stage_id": external_id},
            )
        else:
            logger.warning(
                "HubSpot stage isClosed=true but probability missing; treating as LOST",
                extra={"stage_id": external_id},
            )
        return CRMStageState.LOST
    if probability >= 100.0:
        return CRMStageState.WON
    return CRMStageState.LOST
