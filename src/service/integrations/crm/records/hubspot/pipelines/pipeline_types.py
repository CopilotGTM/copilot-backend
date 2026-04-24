"""Provider-neutral DTOs for pipelines + stages record sync.

These types intentionally avoid ORM/session concerns so the normalizer can run
as a pure function before the writer picks up.
"""

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
    """A HubSpot stage after normalization, ready for the writer.

    `pipeline_id` is intentionally absent — the writer resolves it during Pass B
    against the just-written pipeline rows. Storing it here would require the
    normalizer to know about DB UUIDs.
    """

    external_id: str
    name: str
    label: str
    stage_key: str
    stage_state: CRMStageState
    probability: float | None
    is_closed: bool
    display_order: int | None
    source_modified_at: datetime | None
    data_payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class NormalizedPipeline:
    """A HubSpot deal pipeline after normalization, with stages nested."""

    external_id: str
    name: str
    label: str
    object_type: str  # always "deals" in this phase
    is_default: bool
    display_order: int | None
    source_modified_at: datetime | None
    data_payload: dict[str, Any]  # raw HubSpot pipeline minus the `stages` array
    stages: tuple[NormalizedStage, ...]


@dataclass(slots=True)
class PipelineWriteResult:
    """Counters returned from `HubSpotPipelineWriter.apply()`."""

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
    """End-to-end sync summary for pipelines record sync.

    Feeds both `IntegrationSyncHistory.sync_metadata` and
    `KnowledgeIntegration.connection_metadata.record_sync.pipelines`.
    """

    fetched_pipelines: int = 0
    fetched_stages: int = 0
    pipelines_created: int = 0
    pipelines_updated: int = 0
    pipelines_reactivated: int = 0
    pipelines_deactivated: int = 0
    stages_created: int = 0
    stages_updated: int = 0
    stages_reactivated: int = 0
    stages_deactivated: int = 0
    skipped_pipelines: int = 0
    skipped_stages: int = 0
    error_class: str | None = None
    error_message: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        """Compact payload for sync observability."""
        payload = self.base_metadata()
        payload.update(
            {
                "fetched_pipelines": self.fetched_pipelines,
                "fetched_stages": self.fetched_stages,
                "pipelines_created": self.pipelines_created,
                "pipelines_updated": self.pipelines_updated,
                "pipelines_reactivated": self.pipelines_reactivated,
                "pipelines_deactivated": self.pipelines_deactivated,
                "stages_created": self.stages_created,
                "stages_updated": self.stages_updated,
                "stages_reactivated": self.stages_reactivated,
                "stages_deactivated": self.stages_deactivated,
                "skipped_pipelines": self.skipped_pipelines,
                "skipped_stages": self.skipped_stages,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
