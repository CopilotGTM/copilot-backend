"""Regression tests for deal write stage history and owner create orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStageHistory, CRMDealStatus
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.internal.deal_write_service import DealWriteService


class _FakeOrgSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        """Capture rows that would be pending in the org session."""
        self.added.append(obj)

    async def flush(self) -> None:
        """Mirror AsyncSession.flush without needing a database."""
        return None

    async def execute(self, _stmt: object) -> _DealIdsResult:
        return _DealIdsResult([])


class _DealIdsResult:
    def __init__(self, deal_ids: list[object]) -> None:
        self._deal_ids = deal_ids

    def scalars(self) -> _DealIdsResult:
        return self

    def all(self) -> list[object]:
        return self._deal_ids


def _usable_stage_shape() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(stage_id=uuid4(), stage_state="open"),
        SimpleNamespace(stage_id=uuid4(), stage_state="won"),
        SimpleNamespace(stage_id=uuid4(), stage_state="lost"),
    ]


@pytest.mark.parametrize(
    ("stage_state", "expected_status", "won_is_set", "lost_is_set"),
    [
        ("won", CRMDealStatus.WON, True, False),
        ("lost", CRMDealStatus.LOST, False, True),
    ],
)
def test_create_lifecycle_is_initialized_from_terminal_stage(
    stage_state: str,
    expected_status: CRMDealStatus,
    won_is_set: bool,
    lost_is_set: bool,
) -> None:
    """Creating directly in a terminal stage must produce closed lifecycle state."""
    now = datetime.now(UTC)
    deal = SimpleNamespace(
        deal_status=CRMDealStatus.OPEN,
        close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        properties={"created_via": "crm_import"},
    )

    DealWriteService._initialize_lifecycle_from_stage(
        deal=deal,
        stage=SimpleNamespace(stage_state=stage_state),
        now=now,
    )

    assert deal.deal_status == expected_status
    assert deal.close_date == now
    assert (deal.closed_won_at == now) is won_is_set
    assert (deal.closed_lost_at == now) is lost_is_set
    assert deal.properties["closed_via"] == "deal_create"


@pytest.mark.asyncio
async def test_move_stage_records_previous_stage_context_and_updates_stage_timestamps() -> None:
    """Stage moves preserve from-stage context through DealStageService."""
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._projection = AsyncMock()  # type: ignore[assignment]

    previous_stage_id = uuid4()
    next_stage_id = uuid4()
    previous_stage_entered_at = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    previous_stage_updated_at = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=previous_stage_id,
        stage_name="Discovery",
        stage_entered_at=previous_stage_entered_at,
        stage_updated_at=previous_stage_updated_at,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    stage = SimpleNamespace(stage_id=next_stage_id, name="Proposal")

    service._load_internal_deal = AsyncMock(return_value=deal)
    service._load_pipeline = AsyncMock(
        return_value=SimpleNamespace(pipeline_id=deal.pipeline_id, is_active=True)
    )
    service._load_pipeline_stages = AsyncMock(return_value=_usable_stage_shape())
    service._load_stage = AsyncMock(return_value=stage)

    updated = await service.move_stage(deal.deal_id, next_stage_id)

    assert updated is deal
    assert deal.stage_id == next_stage_id
    assert deal.stage_name == "Proposal"
    assert deal.stage_entered_at is not None
    assert deal.stage_updated_at is not None
    assert deal.stage_entered_at == deal.stage_updated_at
    assert deal.stage_entered_at > previous_stage_entered_at

    history = next(obj for obj in session.added if isinstance(obj, CRMDealStageHistory))
    assert history.stage_id == next_stage_id
    assert not hasattr(history, "external_id")
    assert not hasattr(history, "stage_external_id")
    assert history.from_stage_id == previous_stage_id
    assert not hasattr(history, "from_stage_external_id")
    assert history.source == "api"


@pytest.mark.asyncio
async def test_move_stage_rejects_unusable_internal_pipeline() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    pipeline_id = uuid4()
    integration_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=pipeline_id,
        integration_id=integration_id,
        stage_id=uuid4(),
        stage_name="Qualification",
        stage_entered_at=None,
        stage_updated_at=None,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    stage = SimpleNamespace(stage_id=uuid4(), name="Closed Won", stage_state="won")

    service._load_internal_deal = AsyncMock(return_value=deal)
    service._load_pipeline = AsyncMock(
        return_value=SimpleNamespace(pipeline_id=pipeline_id, is_active=False)
    )
    service._load_pipeline_stages = AsyncMock(return_value=[stage])
    service._load_stage = AsyncMock(return_value=stage)
    service._projection = AsyncMock()
    service._record_mutation_event = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.move_stage(deal.deal_id, stage.stage_id)

    assert exc_info.value.status_code == 409
    assert "open, won, and lost" in exc_info.value.message


@pytest.mark.asyncio
async def test_move_stage_deals_moves_each_deal_from_source_to_destination_stage() -> None:
    session = _FakeOrgSession()
    session.execute = AsyncMock(return_value=_DealIdsResult([uuid4(), uuid4()]))  # type: ignore[attr-defined]
    service = DealWriteService(
        org_session=session,  # type: ignore[arg-type]
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    source_stage_id = uuid4()
    destination_stage_id = uuid4()
    pipeline_id = uuid4()
    service._load_batch_stage = AsyncMock(
        side_effect=[
            SimpleNamespace(stage_id=source_stage_id, pipeline_id=pipeline_id),
            SimpleNamespace(stage_id=destination_stage_id, pipeline_id=pipeline_id),
        ]
    )  # type: ignore[method-assign]
    service.move_stage = AsyncMock(side_effect=[SimpleNamespace(), SimpleNamespace()])  # type: ignore[method-assign]

    moved_count = await service.move_stage_deals(
        source_stage_id,
        destination_stage_id,
        pipeline_id=pipeline_id,
    )

    assert moved_count == 2
    assert service.move_stage.await_count == 2
    assert [call.args[1] for call in service.move_stage.await_args_list] == [
        destination_stage_id,
        destination_stage_id,
    ]


@pytest.mark.asyncio
async def test_move_stage_deals_rejects_same_source_and_destination_stage() -> None:
    service = DealWriteService(
        org_session=_FakeOrgSession(),  # type: ignore[arg-type]
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    stage_id = uuid4()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.move_stage_deals(stage_id, stage_id)

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Source and destination stages must be different"


@pytest.mark.asyncio
async def test_create_deal_sets_initial_stage_timestamps_and_history_source() -> None:
    """Deal creation writes initial stage history with internal identifiers only."""
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._projection = AsyncMock()  # type: ignore[assignment]

    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    pipeline = SimpleNamespace(pipeline_id=pipeline_id, name="Sales")
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery")
    integration = SimpleNamespace(integration_id=integration_id)

    service._get_default_pipeline = AsyncMock(return_value=pipeline)
    service._get_first_stage = AsyncMock(return_value=stage)
    service._load_pipeline_stages = AsyncMock(return_value=_usable_stage_shape())

    with patch(
        "src.service.crm.deals.write_service.InternalCRMSetupService.get_internal_integration",
        new=AsyncMock(return_value=integration),
    ):
        deal = await service.create_deal({"name": "New Deal"})

    assert deal.stage_id == stage_id
    assert not hasattr(deal, "external_id")
    assert not hasattr(deal, "pipeline_external_id")
    assert not hasattr(deal, "stage_external_id")
    assert deal.stage_entered_at is not None
    assert deal.stage_updated_at is not None
    assert deal.stage_entered_at == deal.stage_updated_at

    history = next(obj for obj in session.added if isinstance(obj, CRMDealStageHistory))
    assert history.stage_id == stage_id
    assert not hasattr(history, "external_id")
    assert not hasattr(history, "stage_external_id")
    assert history.from_stage_id is None
    assert not hasattr(history, "from_stage_external_id")
    assert history.source == "api"


@pytest.mark.asyncio
async def test_create_deal_projects_deal_created_and_initial_stage_history() -> None:
    """Internal deal creation projects both source rows created by the writer."""
    session = _FakeOrgSession()
    organisation_id = uuid4()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )

    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    pipeline = SimpleNamespace(pipeline_id=pipeline_id, name="Sales")
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery")
    integration = SimpleNamespace(integration_id=integration_id)
    projection = AsyncMock()

    service._get_default_pipeline = AsyncMock(return_value=pipeline)
    service._get_first_stage = AsyncMock(return_value=stage)
    service._load_pipeline_stages = AsyncMock(return_value=_usable_stage_shape())
    service._record_mutation_event = AsyncMock()  # type: ignore[method-assign]
    service._projection = projection  # type: ignore[assignment]

    with patch(
        "src.service.crm.deals.write_service.InternalCRMSetupService.get_internal_integration",
        new=AsyncMock(return_value=integration),
    ):
        deal = await service.create_deal({"name": "New Deal"})

    history = next(obj for obj in session.added if isinstance(obj, CRMDealStageHistory))
    projection.upsert_deal_created.assert_awaited_once_with(deal)
    projection.upsert_stage_change.assert_awaited_once_with(history, deal=deal)


@pytest.mark.asyncio
async def test_create_deal_stores_preferred_currency_when_currency_provided() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    service._get_default_pipeline = AsyncMock(
        return_value=SimpleNamespace(pipeline_id=pipeline_id, name="Sales")
    )
    service._get_first_stage = AsyncMock(
        return_value=SimpleNamespace(stage_id=stage_id, name="Discovery")
    )
    service._load_pipeline_stages = AsyncMock(return_value=_usable_stage_shape())
    service._record_mutation_event = AsyncMock()  # type: ignore[method-assign]
    service._projection = AsyncMock()  # type: ignore[assignment]
    service._store_preferred_deal_currency = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "src.service.crm.deals.write_service.InternalCRMSetupService.get_internal_integration",
        new=AsyncMock(return_value=SimpleNamespace(integration_id=integration_id)),
    ):
        await service.create_deal({"name": "New Deal", "currency": "inr"})

    service._store_preferred_deal_currency.assert_awaited_once_with("INR")


@pytest.mark.asyncio
async def test_move_stage_projects_created_stage_history() -> None:
    """Internal stage moves project the exact history row created for the transition."""
    session = _FakeOrgSession()
    organisation_id = uuid4()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )

    previous_stage_id = uuid4()
    next_stage_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=previous_stage_id,
        stage_name="Discovery",
        deal_status="open",
        close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        properties={},
        stage_entered_at=datetime(2026, 4, 10, 9, 0, tzinfo=UTC),
        stage_updated_at=datetime(2026, 4, 10, 10, 0, tzinfo=UTC),
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    stage = SimpleNamespace(stage_id=next_stage_id, name="Proposal", stage_state=None)
    projection = AsyncMock()

    service._load_internal_deal = AsyncMock(return_value=deal)
    service._load_pipeline = AsyncMock(
        return_value=SimpleNamespace(pipeline_id=deal.pipeline_id, is_active=True)
    )
    service._load_pipeline_stages = AsyncMock(return_value=_usable_stage_shape())
    service._load_stage = AsyncMock(return_value=stage)
    service._record_mutation_event = AsyncMock()  # type: ignore[method-assign]
    service._projection = projection  # type: ignore[assignment]

    updated = await service.move_stage(deal.deal_id, next_stage_id)

    history = next(obj for obj in session.added if isinstance(obj, CRMDealStageHistory))
    assert updated is deal
    projection.upsert_stage_change.assert_awaited_once_with(history, deal=deal)


@pytest.mark.asyncio
async def test_create_deal_with_owner_syncs_owner_participant_and_created_event_fields() -> None:
    """Initial owner assignment is part of create, not a second owner-change event."""
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._projection = AsyncMock()  # type: ignore[assignment]

    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    owner_user_id = uuid4()
    pipeline = SimpleNamespace(pipeline_id=pipeline_id, name="Sales")
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery")
    integration = SimpleNamespace(integration_id=integration_id)
    crm_user = SimpleNamespace(
        user_id=owner_user_id,
        full_name="Owner User",
        email="owner@example.com",
    )

    service._get_default_pipeline = AsyncMock(return_value=pipeline)
    service._get_first_stage = AsyncMock(return_value=stage)
    service._load_pipeline_stages = AsyncMock(return_value=_usable_stage_shape())
    service._record_mutation_event = AsyncMock()  # type: ignore[method-assign]
    service._owners.sync_owner_participant = AsyncMock()  # type: ignore[method-assign]

    with (
        patch(
            "src.service.crm.deals.write_service.InternalCRMSetupService.get_internal_integration",
            new=AsyncMock(return_value=integration),
        ),
        patch(
            "src.service.crm.deals.write_service.ensure_app_user_crm_identity",
            new=AsyncMock(return_value=crm_user),
        ),
    ):
        deal = await service.create_deal({"name": "New Deal", "owner_user_id": owner_user_id})

    service._owners.sync_owner_participant.assert_awaited_once_with(  # type: ignore[attr-defined]
        deal=deal,
        crm_user=crm_user,
    )
    assert deal.owner_id == owner_user_id
    assert deal.owner_name == "Owner User"
    assert deal.owner_email == "owner@example.com"
    event_kwargs = service._record_mutation_event.await_args.kwargs  # type: ignore[attr-defined]
    assert event_kwargs["event_type"] == "crm.deal.created"
    assert event_kwargs["changed_fields"]["owner_id"]["new"] == owner_user_id


@pytest.mark.asyncio
async def test_create_deal_records_initial_core_fields_without_update_semantics() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._record_mutation_event = AsyncMock()
    service._projection = AsyncMock()  # type: ignore[assignment]

    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    pipeline = SimpleNamespace(pipeline_id=pipeline_id, name="Sales")
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery")
    integration = SimpleNamespace(integration_id=integration_id)

    service._get_default_pipeline = AsyncMock(return_value=pipeline)
    service._get_first_stage = AsyncMock(return_value=stage)
    service._load_pipeline_stages = AsyncMock(return_value=_usable_stage_shape())

    with patch(
        "src.service.crm.deals.write_service.InternalCRMSetupService.get_internal_integration",
        new=AsyncMock(return_value=integration),
    ):
        await service.create_deal(
            {
                "name": "New Deal",
                "amount": 25000,
                "currency": "USD",
                "priority": "high",
                "expected_close_date": datetime(2026, 7, 31, tzinfo=UTC),
            }
        )

    event_kwargs = service._record_mutation_event.await_args.kwargs
    assert event_kwargs["event_type"] == "crm.deal.created"
    assert event_kwargs["changed_fields"]["name"] == {"old": None, "new": "New Deal"}
    assert event_kwargs["changed_fields"]["amount"] == {"old": None, "new": 25000}
    assert event_kwargs["changed_fields"]["currency"] == {"old": None, "new": "USD"}
    assert event_kwargs["changed_fields"]["pipeline_id"]["new"] == pipeline_id
    assert event_kwargs["changed_fields"]["pipeline_name"] == {"old": None, "new": "Sales"}
    assert event_kwargs["changed_fields"]["stage_id"]["new"] == stage_id
    assert event_kwargs["changed_fields"]["stage_name"] == {"old": None, "new": "Discovery"}
