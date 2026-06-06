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
from src.service.integrations.crm.records.hubspot.pipelines.pipeline_types import (
    NormalizedPipeline,
    NormalizedStage,
)
from src.service.integrations.crm.records.hubspot.pipelines.pipeline_writer import (
    HubSpotPipelineWriter,
)


def _stage(external_id: str) -> NormalizedStage:
    return NormalizedStage(
        external_id=external_id,
        label="Qualified",
        name="qualified",
        stage_key="qualified",
        stage_state=CRMStageState.OPEN,
        probability=0.25,
        is_closed=False,
        display_order=1,
        source_modified_at=None,
        data_payload={"id": external_id},
    )


def _pipeline(external_id: str, stage: NormalizedStage) -> NormalizedPipeline:
    return NormalizedPipeline(
        external_id=external_id,
        name="default",
        label="Default",
        object_type="deals",
        is_default=True,
        display_order=0,
        source_modified_at=None,
        data_payload={"id": external_id},
        stages=[stage],
    )


@pytest.mark.asyncio
async def test_writer_upserts_pipeline_and_stage_mappings() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    existing_pipeline = CRMPipeline(
        pipeline_id=pipeline_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="pipe-1",
        name="old",
        object_type="deals",
        is_active=True,
    )
    existing_stage = CRMDealStage(
        stage_id=stage_id,
        pipeline_id=pipeline_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="stage-1",
        name="old",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotPipelineWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_pipelines = AsyncMock(return_value={"pipe-1": existing_pipeline})  # type: ignore[method-assign]
    writer._load_existing_stages = AsyncMock(return_value={"stage-1": existing_stage})  # type: ignore[method-assign]

    result = await writer.apply(pipelines=[_pipeline("pipe-1", _stage("stage-1"))])

    assert result.pipelines_updated == 1
    assert result.stages_updated == 1
    object_types = [
        call.kwargs["external_object_type"]
        for call in mapping_helper.upsert_integration_mapping.call_args_list
    ]
    assert object_types == [CRMSourceObjectType.PIPELINE, CRMSourceObjectType.STAGE]
    mapping_helper.mark_missing_deleted.assert_any_await(
        object_type=CRMSourceObjectType.PIPELINE,
        seen_external_ids={"pipe-1"},
    )
    mapping_helper.mark_missing_deleted.assert_any_await(
        object_type=CRMSourceObjectType.STAGE,
        seen_external_ids={"stage-1"},
    )
