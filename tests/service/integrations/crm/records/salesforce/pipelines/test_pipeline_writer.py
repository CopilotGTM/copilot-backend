from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
    CRMStageState,
)
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_types import (
    NormalizedPipeline,
    NormalizedStage,
)
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_writer import (
    SalesforcePipelineWriter,
)


def _normalized_stage(*, external_id: str, label: str) -> NormalizedStage:
    return NormalizedStage(
        external_id=external_id,
        name=label,
        label=label,
        stage_key=label,
        stage_state=CRMStageState.OPEN,
        probability=20.0,
        forecast_category="Pipeline",
        is_closed=False,
        display_order=10,
        source_modified_at=None,
        data_payload={"label": label},
    )


def _normalized_pipeline(
    *, external_id: str, stages: tuple[NormalizedStage, ...]
) -> NormalizedPipeline:
    return NormalizedPipeline(
        external_id=external_id,
        name="Enterprise_Sales",
        label="Enterprise Sales",
        object_type="deals",
        is_default=False,
        display_order=1,
        source_modified_at=None,
        data_payload={"Id": external_id},
        stages=stages,
    )


@pytest.mark.asyncio
async def test_writer_reactivates_existing_rows_and_deactivates_missing_snapshot_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    pipeline_id = uuid4()

    existing_pipeline = CRMPipeline(
        pipeline_id=pipeline_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="012A",
        name="Old Name",
        label="Old Label",
        object_type="deals",
        is_default=False,
        is_active=False,
    )
    stale_pipeline = CRMPipeline(
        pipeline_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="012STALE",
        name="Stale",
        label="Stale",
        object_type="deals",
        is_default=False,
        is_active=True,
    )
    existing_stage = CRMDealStage(
        stage_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="012A:Prospecting",
        name="Prospecting",
        label="Prospecting",
        stage_key="Prospecting",
        stage_state=CRMStageState.OPEN,
        probability=10.0,
        forecast_category="Pipeline",
        is_closed=False,
        is_active=False,
        pipeline_id=pipeline_id,
    )
    stale_stage = CRMDealStage(
        stage_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="012STALE:Closed Won",
        name="Closed Won",
        label="Closed Won",
        stage_key="Closed Won",
        stage_state=CRMStageState.WON,
        probability=100.0,
        forecast_category="Closed",
        is_closed=True,
        is_active=True,
        pipeline_id=stale_pipeline.pipeline_id,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforcePipelineWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_pipelines = AsyncMock(  # type: ignore[method-assign]
        return_value={"012A": existing_pipeline, "012STALE": stale_pipeline}
    )
    writer._load_existing_stages = AsyncMock(  # type: ignore[method-assign]
        return_value={"012A:Prospecting": existing_stage, "012STALE:Closed Won": stale_stage}
    )

    result = await writer.apply(
        pipelines=[
            _normalized_pipeline(
                external_id="012A",
                stages=(_normalized_stage(external_id="012A:Prospecting", label="Prospecting"),),
            )
        ]
    )

    assert result.pipelines_created == 0
    assert result.pipelines_reactivated == 1
    assert result.pipelines_deactivated == 1
    assert result.stages_created == 0
    assert result.stages_reactivated == 1
    assert result.stages_deactivated == 1
    assert existing_pipeline.is_active is True
    assert existing_pipeline.name == "Enterprise_Sales"
    assert existing_stage.pipeline_id == pipeline_id
    assert stale_pipeline.is_active is False
    assert stale_stage.is_active is False
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    assert mapping_helper.bulk_upsert_integration_mappings.await_count == 2
    pipeline_rows = mapping_helper.bulk_upsert_integration_mappings.await_args_list[0].args[0]
    stage_rows = mapping_helper.bulk_upsert_integration_mappings.await_args_list[1].args[0]
    assert [row.external_id for row in pipeline_rows] == ["012A"]
    assert [row.external_id for row in stage_rows] == ["012A:Prospecting"]
    assert pipeline_rows[0].external_object_type == CRMSourceObjectType.PIPELINE
    assert pipeline_rows[0].local_object_type == CRMSourceObjectType.PIPELINE
    assert stage_rows[0].external_object_type == CRMSourceObjectType.STAGE
    assert stage_rows[0].local_object_type == CRMSourceObjectType.STAGE
    assert [
        call.kwargs["object_type"] for call in mapping_helper.mark_missing_deleted.call_args_list
    ] == [CRMSourceObjectType.PIPELINE, CRMSourceObjectType.STAGE]
