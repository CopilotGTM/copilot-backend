"""Provider-local DTOs for Salesforce pipelines + stages record sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMStageState
from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedStage:
    external_id: str
    name: str
    label: str
    stage_key: str
    stage_state: CRMStageState
    probability: float | None
    forecast_category: str | None
    is_closed: bool
    display_order: int | None
    source_modified_at: datetime | None
    data_payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class NormalizedPipeline:
    external_id: str
    name: str
    label: str
    object_type: str
    is_default: bool
    display_order: int | None
    source_modified_at: datetime | None
    data_payload: dict[str, Any]
    stages: tuple[NormalizedStage, ...]


@dataclass(slots=True)
class PipelineWriteResult:
    pipelines_created: int = 0
    pipelines_updated: int = 0
    pipelines_reactivated: int = 0
    pipelines_deactivated: int = 0
    stages_created: int = 0
    stages_updated: int = 0
    stages_reactivated: int = 0
    stages_deactivated: int = 0


@dataclass(slots=True)
class PipelineSyncResult(RecordSyncResult):
    fetched_record_types: int = 0
    fetched_stage_values: int = 0
    pipelines_created: int = 0
    pipelines_updated: int = 0
    pipelines_reactivated: int = 0
    pipelines_deactivated: int = 0
    stages_created: int = 0
    stages_updated: int = 0
    stages_reactivated: int = 0
    stages_deactivated: int = 0
    skipped_record_types: int = 0
    skipped_stages: int = 0
    unresolved_stage_metadata: int = 0
    error_class: str | None = None
    error_message: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        payload = self.base_metadata()
        payload.update(
            {
                "fetched_record_types": self.fetched_record_types,
                "fetched_stage_values": self.fetched_stage_values,
                "pipelines_created": self.pipelines_created,
                "pipelines_updated": self.pipelines_updated,
                "pipelines_reactivated": self.pipelines_reactivated,
                "pipelines_deactivated": self.pipelines_deactivated,
                "stages_created": self.stages_created,
                "stages_updated": self.stages_updated,
                "stages_reactivated": self.stages_reactivated,
                "stages_deactivated": self.stages_deactivated,
                "skipped_record_types": self.skipped_record_types,
                "skipped_stages": self.skipped_stages,
                "unresolved_stage_metadata": self.unresolved_stage_metadata,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
