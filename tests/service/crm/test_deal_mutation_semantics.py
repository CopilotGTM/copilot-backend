from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService


def _allow_required_custom_fields(service: DealWriteService) -> None:
    service._custom_fields.validate_required_on_create = AsyncMock()
    service._custom_fields.validate_required_not_cleared = AsyncMock()


@pytest.mark.asyncio
async def test_create_deal_does_not_emit_stage_or_owner_semantics() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.created",
        operation="create_deal",
        changed_fields={
            "stage_id": {"old": None, "new": str(uuid4())},
            "owner_id": {"old": None, "new": str(uuid4())},
        },
        summary="Created deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["semantic_event_types"] == ["crm.deal.created"]


@pytest.mark.asyncio
async def test_update_deal_description_is_audit_only() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"description": {"old": "old", "new": "new"}},
        summary="Updated deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["semantic_event_types"] == []


@pytest.mark.asyncio
async def test_load_internal_deal_rejects_invalid_deal_id_before_session_lookup() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.delete_deal("not-a-uuid")  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid deal_id"


@pytest.mark.asyncio
async def test_update_deal_same_value_records_no_changed_fields() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing",
        amount=1000,
        currency="USD",
        deal_status="open",
        close_date=None,
        expected_close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        crm_url=None,
        source_created_at=None,
        last_activity_at=None,
        description=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        probability=None,
        forecast_category=None,
        properties={},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._record_mutation_event = AsyncMock()

    await service.update_deal(deal.deal_id, {"amount": 1000})

    service._record_mutation_event.assert_awaited_once()
    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["changed_fields"] == {}


@pytest.mark.asyncio
async def test_update_deal_tracks_normalized_custom_property_changes() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing",
        amount=1000,
        currency="USD",
        deal_status="open",
        source_created_at=None,
        close_date=None,
        expected_close_date=None,
        description=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        probability=None,
        forecast_category=None,
        properties={},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._validate_custom_properties = AsyncMock(return_value={"our_revenue": 25000000})
    _allow_required_custom_fields(service)
    service._record_mutation_event = AsyncMock()

    await service.update_deal(
        deal.deal_id,
        {"custom_properties": {"Our Revenue": "25000000"}},
    )

    service._record_mutation_event.assert_awaited_once()
    changed_fields = service._record_mutation_event.await_args.kwargs["changed_fields"]
    assert changed_fields["custom_properties.our_revenue"] == {
        "old": None,
        "new": 25000000,
    }


@pytest.mark.asyncio
async def test_update_deal_clears_custom_property_when_validated_value_is_null() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing",
        amount=1000,
        currency="USD",
        deal_status="open",
        source_created_at=None,
        close_date=None,
        expected_close_date=None,
        description=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        probability=None,
        forecast_category=None,
        properties={"our_revenue": 25000000, "created_via": "api"},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._validate_custom_properties = AsyncMock(return_value={"our_revenue": None})
    _allow_required_custom_fields(service)
    service._record_mutation_event = AsyncMock()

    await service.update_deal(
        deal.deal_id,
        {"custom_properties": {"our_revenue": None}},
    )

    assert "our_revenue" not in deal.properties
    changed_fields = service._record_mutation_event.await_args.kwargs["changed_fields"]
    assert changed_fields["custom_properties.our_revenue"] == {
        "old": 25000000,
        "new": None,
    }


@pytest.mark.asyncio
async def test_update_deal_rejects_clearing_required_custom_property() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing",
        amount=1000,
        currency="USD",
        deal_status="open",
        source_created_at=None,
        close_date=None,
        expected_close_date=None,
        description=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        probability=None,
        forecast_category=None,
        properties={"procurement_stage": "legal", "created_via": "api"},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._validate_custom_properties = AsyncMock(return_value={"procurement_stage": None})
    service._custom_fields.validate_required_not_cleared = AsyncMock(
        side_effect=DealWriteServiceError(
            "Required custom fields cannot be cleared: procurement_stage",
            status_code=400,
        )
    )
    service._record_mutation_event = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_deal(
            deal.deal_id,
            {"custom_properties": {"procurement_stage": None}},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Required custom fields cannot be cleared: procurement_stage"
    assert deal.properties["procurement_stage"] == "legal"
    service._record_mutation_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_deal_rejects_fields_that_require_dedicated_actions() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_deal(
            uuid4(),
            {
                "deal_status": "won",
                "closed_reason": "Signed",
                "crm_url": "https://crm.example/deal/1",
                "last_activity_at": "2026-06-01T00:00:00Z",
            },
        )

    assert exc_info.value.status_code == 400
    assert "Use dedicated deal actions" in exc_info.value.message
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,message",
    [
        ({"name": None}, "Deal name cannot be empty"),
        ({"name": "   "}, "Deal name cannot be empty"),
        ({"amount": "many"}, "Deal amount must be a number"),
        ({"amount": -1}, "Deal amount cannot be negative"),
        ({"currency": "US"}, "Deal currency must be a 3-letter code"),
        ({"probability": "high"}, "Deal probability must be a number"),
        ({"probability": 101}, "Deal probability must be between 0 and 100"),
        ({"expected_close_date": "2026-07-31"}, "expected_close_date must be a datetime"),
        ({"source_created_at": "2026-07-31"}, "source_created_at must be a datetime"),
    ],
)
async def test_update_deal_rejects_invalid_core_fields(
    payload: dict[str, object],
    message: str,
) -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_deal(uuid4(), payload)

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == message
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_deal_rejects_lifecycle_and_system_fields() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.create_deal(
            {
                "name": "Unsafe Deal",
                "close_date": "2026-06-01T00:00:00Z",
                "deal_status": "won",
                "closed_reason": "Signed",
                "crm_url": "https://crm.example/deal/1",
                "last_activity_at": "2026-06-01T00:00:00Z",
            },
        )

    assert exc_info.value.status_code == 400
    assert "Use dedicated deal actions" in exc_info.value.message


@pytest.mark.asyncio
async def test_create_deal_rejects_actual_close_date_field() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.create_deal(
            {
                "name": "Unsafe Deal",
                "close_date": "2026-06-01T00:00:00Z",
            },
        )

    assert exc_info.value.status_code == 400
    assert "close_date" in exc_info.value.message


@pytest.mark.asyncio
async def test_create_deal_rejects_unknown_fields_before_setup() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.create_deal({"name": "Unsafe Deal", "amunt": 1000})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Unsupported deal create fields: amunt"


@pytest.mark.asyncio
async def test_create_deal_validates_custom_properties_before_setup(monkeypatch) -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._validate_custom_properties = AsyncMock(
        side_effect=DealWriteServiceError("Unknown custom fields: mystery", status_code=400)
    )

    class UnexpectedSetupService:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("custom properties should be validated before setup")

    monkeypatch.setattr(
        "src.service.crm.deals.write_service.InternalCRMSetupService",
        UnexpectedSetupService,
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.create_deal(
            {
                "name": "Deal with custom fields",
                "custom_properties": {"mystery": "value"},
            }
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Unknown custom fields: mystery"
    service._validate_custom_properties.assert_awaited_once_with({"mystery": "value"})


@pytest.mark.asyncio
async def test_create_deal_enforces_required_custom_fields_before_setup(monkeypatch) -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._validate_custom_properties = AsyncMock(return_value={})
    service._custom_fields.validate_required_on_create = AsyncMock(
        side_effect=DealWriteServiceError(
            "Missing required custom fields: procurement_stage",
            status_code=400,
        )
    )

    class UnexpectedSetupService:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("required custom fields should be validated before setup")

    monkeypatch.setattr(
        "src.service.crm.deals.write_service.InternalCRMSetupService",
        UnexpectedSetupService,
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.create_deal({"name": "Missing custom fields"})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Missing required custom fields: procurement_stage"
    service._custom_fields.validate_required_on_create.assert_awaited_once_with({})


@pytest.mark.asyncio
async def test_create_deal_rejects_unusable_internal_pipeline() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    _allow_required_custom_fields(service)
    pipeline_id = uuid4()
    stage_id = uuid4()
    integration = SimpleNamespace(integration_id=uuid4())
    pipeline = SimpleNamespace(
        pipeline_id=pipeline_id,
        integration_id=integration.integration_id,
        name="Broken Pipeline",
        is_active=True,
    )
    stage = SimpleNamespace(stage_id=stage_id, name="Qualification", stage_state="open")
    service._projection = AsyncMock()
    service._record_mutation_event = AsyncMock()
    service._load_pipeline = AsyncMock(return_value=pipeline)
    service._load_stage = AsyncMock(return_value=stage)
    service._load_pipeline_stages = AsyncMock(
        return_value=[stage, SimpleNamespace(stage_id=uuid4(), stage_state="lost")]
    )

    with (
        patch(
            "src.service.crm.deals.write_service.InternalCRMSetupService.get_internal_integration",
            new=AsyncMock(return_value=integration),
        ),
        pytest.raises(DealWriteServiceError) as exc_info,
    ):
        await service.create_deal(
            {"name": "Unsafe Deal", "pipeline_id": pipeline_id, "stage_id": stage_id}
        )

    assert exc_info.value.status_code == 409
    assert "open, won, and lost" in exc_info.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,message",
    [
        ({}, "Deal name is required"),
        ({"name": "   "}, "Deal name is required"),
        ({"name": "Unsafe Deal", "amount": "many"}, "Deal amount must be a number"),
        ({"name": "Unsafe Deal", "amount": -1}, "Deal amount cannot be negative"),
        ({"name": "Unsafe Deal", "currency": "US"}, "Deal currency must be a 3-letter code"),
        ({"name": "Unsafe Deal", "probability": "high"}, "Deal probability must be a number"),
        ({"name": "Unsafe Deal", "probability": 101}, "Deal probability must be between 0 and 100"),
        (
            {"name": "Unsafe Deal", "expected_close_date": "2026-07-31"},
            "expected_close_date must be a datetime",
        ),
        (
            {"name": "Unsafe Deal", "source_created_at": "2026-07-31"},
            "source_created_at must be a datetime",
        ),
        ({"name": "Unsafe Deal", "pipeline_id": "not-a-uuid"}, "Invalid pipeline_id"),
        ({"name": "Unsafe Deal", "stage_id": "not-a-uuid"}, "Invalid stage_id"),
        ({"name": "Unsafe Deal", "owner_user_id": "not-a-uuid"}, "Invalid owner_user_id"),
        ({"name": "Unsafe Deal", "account": {"website": 123}}, "website must be a string"),
        (
            {"name": "Unsafe Deal", "team_members": [{"user_id": "not-a-uuid"}]},
            "Invalid team_members.user_id",
        ),
        (
            {"name": "Unsafe Deal", "team_members": [{}]},
            "Invalid team_members.user_id",
        ),
        (
            {"name": "Unsafe Deal", "contact_participants": [{"contact_id": "not-a-uuid"}]},
            "Invalid contact_participants.contact_id",
        ),
        (
            {"name": "Unsafe Deal", "contact_participants": [{}]},
            "Invalid contact_participants.contact_id",
        ),
        (
            {"name": "Unsafe Deal", "primary_contact_name": 123},
            "primary_contact_name must be a string",
        ),
        (
            {
                "name": "Unsafe Deal",
                "primary_contact_name": "Jane Doe",
                "primary_contact_email": "not-an-email",
            },
            "primary_contact_email must be a valid email",
        ),
    ],
)
async def test_create_deal_rejects_invalid_core_fields(
    payload: dict[str, object],
    message: str,
) -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.create_deal(payload)

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == message
