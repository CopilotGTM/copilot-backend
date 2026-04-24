"""Salesforce Opportunity RecordType + StageName metadata normalizer."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMStageState
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_types import (
    NormalizedPipeline,
    NormalizedStage,
)

logger = get_logger(__name__)


def normalize_salesforce_pipelines(
    *,
    record_types: Iterable[dict[str, Any]],
    stage_values_by_record_type: Mapping[str, list[dict[str, Any]]],
    opportunity_stages: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedPipeline], int, int, int]:
    selected_record_types = _select_record_types_for_sync(record_types)
    stage_meta_index = _build_stage_metadata_index(opportunity_stages)

    pipelines: list[NormalizedPipeline] = []
    skipped_record_types = 0
    skipped_stages = 0
    unresolved_stage_metadata = 0

    for raw_record_type in selected_record_types:
        record_type_id = raw_record_type.get("Id")
        if not isinstance(record_type_id, str) or not record_type_id:
            skipped_record_types += 1
            logger.warning("Skipping Salesforce record type with no usable Id")
            continue

        is_master = bool(raw_record_type.get("IsMaster"))
        pipeline_external_id = "master" if is_master else record_type_id
        label = _coerce_text(raw_record_type.get("Name"), fallback=pipeline_external_id)
        name = _coerce_text(raw_record_type.get("DeveloperName"), fallback=label)

        stages: list[NormalizedStage] = []
        for raw_stage_value in stage_values_by_record_type.get(record_type_id, []):
            normalized, unresolved = _normalize_stage(
                raw_stage_value=raw_stage_value,
                pipeline_external_id=pipeline_external_id,
                stage_meta_index=stage_meta_index,
            )
            if normalized is None:
                skipped_stages += 1
                continue
            stages.append(normalized)
            unresolved_stage_metadata += 1 if unresolved else 0

        pipelines.append(
            NormalizedPipeline(
                external_id=pipeline_external_id,
                name=name,
                label=label,
                object_type="deals",
                is_default=is_master,
                display_order=None,
                source_modified_at=None,
                data_payload=dict(raw_record_type),
                stages=tuple(stages),
            )
        )

    return pipelines, skipped_record_types, skipped_stages, unresolved_stage_metadata


def _select_record_types_for_sync(
    record_types: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    active = [row for row in record_types if bool(row.get("IsActive", True))]
    return active


def _normalize_stage(
    *,
    raw_stage_value: dict[str, Any],
    pipeline_external_id: str,
    stage_meta_index: Mapping[str, dict[str, Any]],
) -> tuple[NormalizedStage | None, bool]:
    stage_value = raw_stage_value.get("value")
    if not isinstance(stage_value, str) or not stage_value:
        logger.warning("Skipping Salesforce StageName entry with no usable value")
        return None, False

    label = _coerce_text(raw_stage_value.get("label"), fallback=stage_value)
    metadata = (
        stage_meta_index.get(stage_value.casefold()) or stage_meta_index.get(label.casefold()) or {}
    )
    unresolved = not bool(metadata)

    stage_state = _derive_stage_state(metadata)
    probability = _coerce_probability(metadata.get("DefaultProbability"))
    is_closed = bool(metadata.get("IsClosed")) if metadata else False
    display_order = _coerce_int(metadata.get("SortOrder"))
    forecast_category = _coerce_text_or_none(metadata.get("ForecastCategory"))

    if unresolved:
        logger.warning(
            "Salesforce StageName value had no OpportunityStage metadata match; defaulting to OPEN",
            extra={"stage_value": stage_value, "pipeline_external_id": pipeline_external_id},
        )

    return (
        NormalizedStage(
            external_id=f"{pipeline_external_id}:{stage_value}",
            name=label,
            label=label,
            stage_key=stage_value,
            stage_state=stage_state,
            probability=probability,
            forecast_category=forecast_category,
            is_closed=is_closed,
            display_order=display_order,
            source_modified_at=None,
            data_payload={
                "picklist": dict(raw_stage_value),
                "opportunity_stage": dict(metadata) if metadata else None,
            },
        ),
        unresolved,
    )


def _build_stage_metadata_index(
    opportunity_stages: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in opportunity_stages:
        if not isinstance(row, dict):
            continue
        for key_name in ("ApiName", "MasterLabel"):
            key_value = row.get(key_name)
            if isinstance(key_value, str) and key_value:
                index[key_value.casefold()] = row
    return index


def _derive_stage_state(metadata: Mapping[str, Any]) -> CRMStageState:
    if bool(metadata.get("IsWon")):
        return CRMStageState.WON
    if bool(metadata.get("IsClosed")):
        return CRMStageState.LOST
    if bool(metadata.get("IsActive")):
        return CRMStageState.OPEN
    return CRMStageState.OPEN


def _coerce_text(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and value:
        return value
    return fallback


def _coerce_text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_probability(value: Any) -> float | None:
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    if as_float < 0.0:
        return 0.0
    if as_float > 100.0:
        return 100.0
    return as_float
