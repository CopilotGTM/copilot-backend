from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.api.routers.crm.pipelines_handler import _build_pipeline_item, _build_stage_item
from src.api.schemas.crm_pipeline_write_models import (
    ClonePipelineRequest,
    CreatePipelineRequest,
    CreateStageRequest,
    ReorderStagesRequest,
    StageInput,
    UpdatePipelineRequest,
    UpdateStageRequest,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.service.crm.internal.pipeline_write_service import (
    PipelineWriteService,
    PipelineWriteServiceError,
)


class _PipelineSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = 0
        self.committed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def refresh(self, _obj: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1


def _mutation_events(session: _PipelineSession) -> list[CRMDealMutationEvent]:
    return [obj for obj in session.added if isinstance(obj, CRMDealMutationEvent)]


def _service(session: _PipelineSession | None = None) -> PipelineWriteService:
    return PipelineWriteService(
        session=session or _PipelineSession(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_create_pipeline_keeps_empty_internal_pipeline_unusable() -> None:
    session = _PipelineSession()
    service = _service(session)
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._pipeline_name_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]
    service._load_pipeline_stages = AsyncMock(return_value=[])  # type: ignore[method-assign]

    pipeline, stages = await service.create_pipeline(CreatePipelineRequest(name="Enterprise"))

    assert stages == []
    assert pipeline.is_active is False
    assert pipeline.properties["usable_for_internal_deals"] is False


@pytest.mark.asyncio
async def test_create_pipeline_rejects_default_without_required_stage_states() -> None:
    service = _service()
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._pipeline_name_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]
    service._load_pipeline_stages = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service._unset_existing_default = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.create_pipeline(
            CreatePipelineRequest(
                name="Broken",
                set_as_default=True,
                stages=[
                    StageInput(name="Qualification", stage_state="open"),
                    StageInput(name="Closed Lost", stage_state="lost"),
                ],
            )
        )

    assert exc_info.value.status_code == 400
    assert "open, won, and lost" in exc_info.value.message


@pytest.mark.asyncio
async def test_create_pipeline_rejects_duplicate_initial_stage_keys() -> None:
    service = _service()
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._pipeline_name_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.create_pipeline(
            CreatePipelineRequest(
                name="Enterprise",
                stages=[
                    StageInput(name="Closed Won", stage_state="won"),
                    StageInput(name="Closed  Won", stage_state="won"),
                ],
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "A stage named 'Closed  Won' already exists in this pipeline"


@pytest.mark.asyncio
async def test_clone_pipeline_reuses_existing_stage_shape() -> None:
    service = _service()
    source_pipeline_id = uuid4()
    integration_id = uuid4()
    source_stages = [
        SimpleNamespace(
            name="Discovery",
            label="Discovery",
            stage_state="open",
            probability=25,
            display_order=1,
        ),
        SimpleNamespace(
            name="Closed Won",
            label="Closed Won",
            stage_state="won",
            probability=100,
            display_order=2,
        ),
        SimpleNamespace(
            name="Closed Lost",
            label="Closed Lost",
            stage_state="lost",
            probability=0,
            display_order=3,
        ),
    ]
    service._get_internal_integration_id = AsyncMock(return_value=integration_id)  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(pipeline_id=source_pipeline_id)
    )
    service._load_pipeline_stages = AsyncMock(return_value=source_stages)  # type: ignore[method-assign]
    service.create_pipeline = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(name="Enterprise Copy"), [])
    )

    await service.clone_pipeline(
        source_pipeline_id,
        ClonePipelineRequest(name="Enterprise Copy", set_as_default=True),
    )

    service.create_pipeline.assert_awaited_once()
    clone_request = service.create_pipeline.await_args.args[0]
    assert clone_request.name == "Enterprise Copy"
    assert clone_request.set_as_default is True
    assert [
        (stage.name, stage.stage_state, stage.probability, stage.display_order)
        for stage in clone_request.stages
    ] == [
        ("Discovery", "open", 25, 1),
        ("Closed Won", "won", 100, 2),
        ("Closed Lost", "lost", 0, 3),
    ]


def test_stage_builder_applies_closed_stage_probability_presets() -> None:
    session = _PipelineSession()
    service = _service(session)
    now = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    pipeline = SimpleNamespace(pipeline_id=uuid4())
    integration_id = uuid4()

    won_stage = service._build_stage_row(  # noqa: SLF001
        pipeline,
        StageInput(name="Signed", stage_state="won"),
        integration_id,
        now,
    )
    lost_stage = service._build_stage_row(  # noqa: SLF001
        pipeline,
        StageInput(name="Lost", stage_state="lost"),
        integration_id,
        now,
    )

    assert won_stage.probability == 100
    assert lost_stage.probability == 0


def test_pipeline_response_includes_last_changed_metadata() -> None:
    actor_id = uuid4()
    event = SimpleNamespace(
        occurred_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
        actor_user_id=actor_id,
        source="api",
        event_type="crm.settings.pipeline.updated",
    )
    pipeline = SimpleNamespace(
        pipeline_id=uuid4(),
        name="Enterprise",
        label="Enterprise",
        is_default=False,
        is_active=True,
        display_order=1,
        updated_at=datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
    )

    item = _build_pipeline_item(pipeline, [], last_changed_event=event)

    assert item.last_changed is not None
    assert item.last_changed.changed_at == event.occurred_at
    assert item.last_changed.actor_user_id == actor_id


def test_pipeline_stage_response_includes_last_changed_metadata() -> None:
    actor_id = uuid4()
    event = SimpleNamespace(
        occurred_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
        actor_user_id=actor_id,
        source="api",
        event_type="crm.settings.stage.updated",
    )
    stage = SimpleNamespace(
        stage_id=uuid4(),
        name="Discovery",
        label="Discovery",
        stage_state="open",
        probability=25,
        is_closed=False,
        display_order=1,
        updated_at=datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
    )

    item = _build_stage_item(stage, last_changed_event=event)

    assert item.last_changed is not None
    assert item.last_changed.event_type == "crm.settings.stage.updated"
    assert item.updated_at == stage.updated_at


@pytest.mark.asyncio
async def test_delete_stage_rejects_removing_last_required_state() -> None:
    service = _service()
    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    target_stage = SimpleNamespace(stage_id=stage_id, stage_state="won")
    service._get_internal_integration_id = AsyncMock(return_value=integration_id)  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=SimpleNamespace(pipeline_id=pipeline_id))  # type: ignore[method-assign]
    service._load_stage = AsyncMock(return_value=target_stage)  # type: ignore[method-assign]
    service._count_deals_for_stage = AsyncMock(return_value=0)  # type: ignore[method-assign]
    service._load_pipeline_stages = AsyncMock(
        return_value=[
            SimpleNamespace(stage_id=uuid4(), stage_state="open", is_deleted=False),
            target_stage,
            SimpleNamespace(stage_id=uuid4(), stage_state="lost", is_deleted=False),
        ]
    )  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.delete_stage(pipeline_id, stage_id)

    assert exc_info.value.status_code == 409
    assert "last won stage" in exc_info.value.message


@pytest.mark.asyncio
async def test_create_stage_rejects_duplicate_name_or_key() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    integration_id = uuid4()
    service._get_internal_integration_id = AsyncMock(return_value=integration_id)  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=SimpleNamespace(pipeline_id=pipeline_id))  # type: ignore[method-assign]
    service._stage_name_or_key_exists = AsyncMock(return_value=True)  # type: ignore[attr-defined]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.create_stage(
            pipeline_id,
            CreateStageRequest(name="Closed  Won", display_order=7),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "A stage named 'Closed  Won' already exists in this pipeline"
    assert session.added == []


@pytest.mark.asyncio
async def test_update_stage_rejects_duplicate_rename() -> None:
    service = _service()
    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    service._get_internal_integration_id = AsyncMock(return_value=integration_id)  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=SimpleNamespace(pipeline_id=pipeline_id))  # type: ignore[method-assign]
    service._load_stage = AsyncMock(
        return_value=SimpleNamespace(
            stage_id=stage_id,
            name="Discovery",
            label="Discovery",
            updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
    )  # type: ignore[method-assign]
    service._stage_name_or_key_exists = AsyncMock(return_value=True)  # type: ignore[attr-defined]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.update_stage(
            pipeline_id,
            stage_id,
            UpdateStageRequest(
                name="Closed Won",
                if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "A stage named 'Closed Won' already exists in this pipeline"


@pytest.mark.asyncio
async def test_update_stage_renames_without_rewriting_stage_key() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    stage = SimpleNamespace(
        stage_id=stage_id,
        name="Discovery",
        label="Discovery",
        stage_key="discovery",
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    service._get_internal_integration_id = AsyncMock(return_value=integration_id)  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=SimpleNamespace(pipeline_id=pipeline_id))  # type: ignore[method-assign]
    service._load_stage = AsyncMock(return_value=stage)  # type: ignore[method-assign]
    service._stage_name_or_key_exists = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    session.execute = AsyncMock()  # type: ignore[method-assign]

    updated = await service.update_stage(
        pipeline_id,
        stage_id,
        UpdateStageRequest(
            name="Technical Validation",
            if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        ),
    )

    assert updated.name == "Technical Validation"
    assert updated.label == "Technical Validation"
    assert updated.stage_key == "discovery"
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_update_pipeline_records_settings_mutation_event() -> None:
    session = _PipelineSession()
    session.execute = AsyncMock()  # type: ignore[method-assign]
    service = _service(session)
    pipeline_id = uuid4()
    pipeline = SimpleNamespace(
        pipeline_id=pipeline_id,
        name="Enterprise",
        label="Enterprise",
        is_default=False,
        is_active=True,
        display_order=1,
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=pipeline)  # type: ignore[method-assign]

    await service.update_pipeline(
        pipeline_id,
        UpdatePipelineRequest(
            label="Enterprise Sales",
            if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        ),
    )

    event = _mutation_events(session)[0]
    assert event.entity_type == "pipeline"
    assert event.entity_id == pipeline_id
    assert event.operation == "pipeline.update"
    assert event.changed_fields["label"] == {"old": "Enterprise", "new": "Enterprise Sales"}
    assert event.actor_user_id == service.user_id


@pytest.mark.asyncio
async def test_update_stage_records_settings_mutation_event() -> None:
    session = _PipelineSession()
    session.execute = AsyncMock()  # type: ignore[method-assign]
    service = _service(session)
    pipeline_id = uuid4()
    stage_id = uuid4()
    stage = SimpleNamespace(
        stage_id=stage_id,
        name="Discovery",
        label="Discovery",
        stage_key="discovery",
        probability=25,
        display_order=1,
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(pipeline_id=pipeline_id)
    )
    service._load_stage = AsyncMock(return_value=stage)  # type: ignore[method-assign]

    await service.update_stage(
        pipeline_id,
        stage_id,
        UpdateStageRequest(
            probability=40,
            if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        ),
    )

    event = _mutation_events(session)[0]
    assert event.entity_type == "stage"
    assert event.entity_id == stage_id
    assert event.operation == "stage.update"
    assert event.changed_fields["probability"] == {"old": 25, "new": 40}


@pytest.mark.asyncio
async def test_delete_pipeline_records_stage_delete_events_for_cascade() -> None:
    session = _PipelineSession()
    session.execute = AsyncMock()  # type: ignore[method-assign]
    service = _service(session)
    pipeline_id = uuid4()
    stage = SimpleNamespace(stage_id=uuid4(), name="Discovery", is_deleted=False)
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            pipeline_id=pipeline_id,
            name="Enterprise",
            is_default=False,
            is_deleted=False,
        )
    )
    service._count_deals_for_pipeline = AsyncMock(return_value=0)  # type: ignore[method-assign]
    service._load_pipeline_stages = AsyncMock(return_value=[stage])  # type: ignore[method-assign]

    await service.delete_pipeline(pipeline_id)

    events = _mutation_events(session)
    assert [event.entity_type for event in events] == ["pipeline", "stage"]
    assert events[1].entity_id == stage.stage_id
    assert events[1].operation == "stage.delete"


@pytest.mark.asyncio
async def test_update_pipeline_requires_version_precondition() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    pipeline = SimpleNamespace(
        pipeline_id=pipeline_id,
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        display_order=None,
    )
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=pipeline)  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.update_pipeline(
            pipeline_id,
            UpdatePipelineRequest.model_construct(display_order=5, if_updated_at=None),
        )

    assert exc_info.value.status_code == 400
    assert "if_updated_at is required" in exc_info.value.message
    assert pipeline.display_order is None
    assert session.committed == 0


@pytest.mark.asyncio
async def test_update_stage_requires_version_precondition() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    stage_id = uuid4()
    stage = SimpleNamespace(
        stage_id=stage_id,
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        probability=25,
    )
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=SimpleNamespace(pipeline_id=pipeline_id))  # type: ignore[method-assign]
    service._load_stage = AsyncMock(return_value=stage)  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.update_stage(
            pipeline_id,
            stage_id,
            UpdateStageRequest.model_construct(probability=50, if_updated_at=None),
        )

    assert exc_info.value.status_code == 400
    assert "if_updated_at is required" in exc_info.value.message
    assert stage.probability == 25
    assert session.committed == 0


@pytest.mark.asyncio
async def test_update_pipeline_rejects_stale_version_precondition() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    pipeline = SimpleNamespace(
        pipeline_id=pipeline_id,
        updated_at=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        name="Enterprise",
        label="Enterprise",
        display_order=None,
    )
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=pipeline)  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.update_pipeline(
            pipeline_id,
            UpdatePipelineRequest(
                display_order=5,
                if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            ),
        )

    assert exc_info.value.status_code == 409
    assert "Pipeline changed since it was loaded" in exc_info.value.message
    assert pipeline.display_order is None
    assert session.committed == 0


@pytest.mark.asyncio
async def test_update_stage_rejects_stale_version_precondition() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    stage = SimpleNamespace(
        stage_id=stage_id,
        updated_at=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        name="Discovery",
        label="Discovery",
        probability=25,
    )
    service._get_internal_integration_id = AsyncMock(return_value=integration_id)  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=SimpleNamespace(pipeline_id=pipeline_id))  # type: ignore[method-assign]
    service._load_stage = AsyncMock(return_value=stage)  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.update_stage(
            pipeline_id,
            stage_id,
            UpdateStageRequest(
                probability=50,
                if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            ),
        )

    assert exc_info.value.status_code == 409
    assert "Stage changed since it was loaded" in exc_info.value.message
    assert stage.probability == 25
    assert session.committed == 0


@pytest.mark.asyncio
async def test_reorder_stages_updates_all_orders_and_pipeline_version() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    integration_id = uuid4()
    loaded_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    pipeline = SimpleNamespace(pipeline_id=pipeline_id, updated_at=loaded_at)
    first_stage = SimpleNamespace(stage_id=uuid4(), display_order=1)
    second_stage = SimpleNamespace(stage_id=uuid4(), display_order=2)
    third_stage = SimpleNamespace(stage_id=uuid4(), display_order=3)
    service._get_internal_integration_id = AsyncMock(return_value=integration_id)  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=pipeline)  # type: ignore[method-assign]
    service._load_pipeline_stages = AsyncMock(  # type: ignore[method-assign]
        return_value=[first_stage, second_stage, third_stage]
    )

    reordered = await service.reorder_stages(
        pipeline_id,
        ReorderStagesRequest(
            stage_ids=[third_stage.stage_id, first_stage.stage_id, second_stage.stage_id],
            if_updated_at=loaded_at,
        ),
    )

    assert [stage.stage_id for stage in reordered] == [
        third_stage.stage_id,
        first_stage.stage_id,
        second_stage.stage_id,
    ]
    assert [stage.display_order for stage in reordered] == [1, 2, 3]
    assert pipeline.updated_at > loaded_at
    assert session.flushed >= 1
    assert session.committed == 1


@pytest.mark.asyncio
async def test_reorder_stages_rejects_stale_pipeline_version() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    pipeline = SimpleNamespace(
        pipeline_id=pipeline_id,
        updated_at=datetime(2026, 7, 5, 10, 5, tzinfo=UTC),
    )
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(return_value=pipeline)  # type: ignore[method-assign]
    service._load_pipeline_stages = AsyncMock(return_value=[])  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.reorder_stages(
            pipeline_id,
            ReorderStagesRequest(
                stage_ids=[uuid4()],
                if_updated_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
            ),
        )

    assert exc_info.value.status_code == 409
    assert "changed since it was loaded" in exc_info.value.message
    service._load_pipeline_stages.assert_not_called()
    assert session.committed == 0


@pytest.mark.asyncio
async def test_reorder_stages_rejects_partial_or_foreign_stage_ids() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    loaded_at = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    first_stage = SimpleNamespace(stage_id=uuid4(), display_order=1)
    second_stage = SimpleNamespace(stage_id=uuid4(), display_order=2)
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(pipeline_id=pipeline_id, updated_at=loaded_at)
    )
    service._load_pipeline_stages = AsyncMock(  # type: ignore[method-assign]
        return_value=[first_stage, second_stage]
    )

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.reorder_stages(
            pipeline_id,
            ReorderStagesRequest(
                stage_ids=[first_stage.stage_id, uuid4()],
                if_updated_at=loaded_at,
            ),
        )

    assert exc_info.value.status_code == 409
    assert "include every non-deleted stage" in exc_info.value.message
    assert [first_stage.display_order, second_stage.display_order] == [1, 2]
    assert session.committed == 0


@pytest.mark.asyncio
async def test_reorder_stages_rejects_duplicate_stage_ids() -> None:
    session = _PipelineSession()
    service = _service(session)
    pipeline_id = uuid4()
    stage_id = uuid4()
    loaded_at = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    service._get_internal_integration_id = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._load_pipeline = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(pipeline_id=pipeline_id, updated_at=loaded_at)
    )
    service._load_pipeline_stages = AsyncMock(return_value=[])  # type: ignore[method-assign]

    with pytest.raises(PipelineWriteServiceError) as exc_info:
        await service.reorder_stages(
            pipeline_id,
            ReorderStagesRequest(stage_ids=[stage_id, stage_id], if_updated_at=loaded_at),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Duplicate stage ids are not allowed"
    service._load_pipeline_stages.assert_not_called()
    assert session.committed == 0
