from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.api.routers.crm.custom_fields_handler import _to_response_item
from src.api.schemas.crm_custom_field_models import (
    CreateCustomFieldRequest,
    ReorderCustomFieldsRequest,
    UpdateCustomFieldRequest,
)
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.service.crm.deals.custom_field_service import DealCustomFieldWriteService
from src.service.crm.internal.custom_field_service import CustomFieldService
from src.service.crm.internal.custom_field_write_service import (
    CustomFieldWriteService,
    CustomFieldWriteServiceError,
)
from src.service.crm.internal.deal_write_service import DealWriteService, DealWriteServiceError
from src.service.crm.schema.deal_schema_service import DealSchemaService


class _FieldRowsResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_FieldRowsResult":
        return self

    def all(self) -> list[object]:
        return self._rows


class _CustomFieldSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = 0
        self.committed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, _obj: object) -> None:
        return None

    async def close(self) -> None:
        return None


def _mutation_events(session: _CustomFieldSession) -> list[CRMDealMutationEvent]:
    return [obj for obj in session.added if isinstance(obj, CRMDealMutationEvent)]


def _custom_field(
    key: str,
    *,
    data_type: CRMCustomFieldDataType = CRMCustomFieldDataType.TEXT,
    required: bool = False,
    active: bool = True,
    deleted: bool = False,
    source: CRMCustomFieldSource = CRMCustomFieldSource.INTERNAL,
    options: dict | None = None,
) -> object:
    return type(
        "Field",
        (),
        {
            "key": key,
            "data_type": data_type,
            "required": required,
            "is_active": active,
            "is_deleted": deleted,
            "field_source": source,
            "options": options or {},
        },
    )()


def _custom_field_row(
    *,
    field_id,
    updated_at: datetime,
    display_order: int | None = None,
    entity: CRMCustomFieldEntity = CRMCustomFieldEntity.DEAL,
    source: CRMCustomFieldSource = CRMCustomFieldSource.INTERNAL,
):
    return type(
        "FieldRow",
        (),
        {
            "field_id": field_id,
            "entity_type": entity,
            "field_source": source,
            "updated_at": updated_at,
            "display_order": display_order,
        },
    )()


@pytest.mark.asyncio
async def test_custom_field_write_service_list_fields_filters_internal_source() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )

    service = CustomFieldWriteService(
        session=session,
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    await service.list_fields(entity=CRMCustomFieldEntity.DEAL)

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text


@pytest.mark.asyncio
async def test_custom_field_write_service_load_field_filters_internal_source() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalar_one_or_none": lambda self: None})()
    )
    service = CustomFieldWriteService(
        session=session,
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    with pytest.raises(CustomFieldWriteServiceError):
        await service.get_field(uuid4())

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text


@pytest.mark.asyncio
async def test_legacy_custom_field_service_filters_internal_source_for_field_map() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )
    service = CustomFieldService(session=session, organisation_id=uuid4(), actor_user_id=uuid4())

    await service.get_field_map(CRMCustomFieldEntity.DEAL, ["priority"])

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text


@pytest.mark.asyncio
async def test_deal_write_service_custom_property_validation_filters_internal_source() -> None:
    org_session = AsyncMock()
    org_session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )
    service = DealWriteService(
        org_session=org_session,
        master_session=AsyncMock(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    with pytest.raises(DealWriteServiceError):
        await service._validate_custom_properties({"priority": "High"})  # noqa: SLF001

    stmt = org_session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text


def test_deal_custom_property_key_normalizer_accepts_custom_prefix() -> None:
    assert DealCustomFieldWriteService.normalize_custom_key(" custom_Our Revenue ") == "our_revenue"


@pytest.mark.asyncio
async def test_deal_custom_property_required_create_rejects_missing_internal_fields() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field("procurement_stage", required=True),
                _custom_field(
                    "seat_count", data_type=CRMCustomFieldDataType.INTEGER, required=True
                ),
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_required_on_create({"procurement_stage": "Legal"})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Missing required custom fields: seat_count"


@pytest.mark.asyncio
async def test_deal_custom_property_required_create_treats_typed_falsey_values_as_present() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field(
                    "seat_count", data_type=CRMCustomFieldDataType.INTEGER, required=True
                ),
                _custom_field(
                    "exec_sponsor",
                    data_type=CRMCustomFieldDataType.BOOLEAN,
                    required=True,
                ),
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    await service.validate_required_on_create({"seat_count": 0, "exec_sponsor": False})


@pytest.mark.asyncio
async def test_deal_custom_property_required_create_ignores_inactive_provider_and_formula_fields() -> (
    None
):
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field("archived", required=True, active=False),
                _custom_field(
                    "provider_required", required=True, source=CRMCustomFieldSource.PROVIDER
                ),
                _custom_field(
                    "formula_score", data_type=CRMCustomFieldDataType.FORMULA, required=True
                ),
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    await service.validate_required_on_create({})


@pytest.mark.asyncio
async def test_deal_custom_property_required_update_rejects_clearing_required_field() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult([_custom_field("procurement_stage", required=True)])
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_required_not_cleared({"procurement_stage": None})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Required custom fields cannot be cleared: procurement_stage"


@pytest.mark.asyncio
async def test_deal_custom_property_required_terminal_transition_rejects_missing_values() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult([_custom_field("procurement_stage", required=True)])
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_required_for_terminal_transition({})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Missing required custom fields: procurement_stage"


@pytest.mark.asyncio
async def test_deal_custom_property_validation_rejects_duplicate_normalized_keys() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_custom_properties(
            {
                "Our Revenue": "100",
                "custom_our_revenue": "200",
            }
        )

    assert exc_info.value.status_code == 400
    assert "Duplicate custom field keys: our_revenue" in exc_info.value.message
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_deal_custom_property_validation_rejects_empty_normalized_keys() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_custom_properties({"custom_": "100"})

    assert exc_info.value.status_code == 400
    assert "Invalid custom field keys: custom_" in exc_info.value.message
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_deal_custom_property_validation_accepts_exact_returned_custom_prefixed_key() -> None:
    org_id = uuid4()
    field = type(
        "Field",
        (),
        {
            "key": "custom_budget",
            "data_type": CRMCustomFieldDataType.CURRENCY,
            "field_source": CRMCustomFieldSource.INTERNAL,
        },
    )()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: [field]})()
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=org_id)

    result = await service.validate_custom_properties({"custom_budget": "$1,250"})

    assert result == {"custom_budget": 1250.0}


@pytest.mark.asyncio
async def test_deal_custom_property_select_validation_rejects_unknown_option() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field(
                    "procurement_stage",
                    data_type=CRMCustomFieldDataType.SELECT,
                    options={"values": [{"value": "legal"}, {"value": "security"}]},
                )
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_custom_properties({"procurement_stage": "finance"})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid option for custom field 'procurement_stage'"


@pytest.mark.asyncio
async def test_deal_custom_property_select_validation_accepts_configured_option() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field(
                    "procurement_stage",
                    data_type=CRMCustomFieldDataType.SELECT,
                    options={"values": [{"value": "legal"}, {"value": "security"}]},
                )
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    result = await service.validate_custom_properties({"procurement_stage": " legal "})

    assert result == {"procurement_stage": "legal"}


@pytest.mark.asyncio
async def test_deal_custom_property_multiselect_validation_rejects_unknown_option() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field(
                    "buying_roles",
                    data_type=CRMCustomFieldDataType.MULTISELECT,
                    options={"values": ["economic_buyer", "technical_buyer"]},
                )
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_custom_properties(
            {"buying_roles": ["economic_buyer", "unknown_role"]}
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid option for custom field 'buying_roles'"


@pytest.mark.asyncio
async def test_deal_custom_property_multiselect_validation_accepts_configured_options() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field(
                    "buying_roles",
                    data_type=CRMCustomFieldDataType.MULTISELECT,
                    options={"values": ["economic_buyer", "technical_buyer"]},
                )
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    result = await service.validate_custom_properties(
        {"buying_roles": [" economic_buyer ", "technical_buyer"]}
    )

    assert result == {"buying_roles": ["economic_buyer", "technical_buyer"]}


@pytest.mark.asyncio
async def test_deal_custom_property_multiselect_validation_allows_empty_value() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field(
                    "buying_roles",
                    data_type=CRMCustomFieldDataType.MULTISELECT,
                    options={"values": ["economic_buyer", "technical_buyer"]},
                )
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    result = await service.validate_custom_properties({"buying_roles": []})

    assert result == {"buying_roles": []}


@pytest.mark.asyncio
async def test_deal_custom_property_select_validation_rejects_removed_option() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _custom_field(
                    "procurement_stage",
                    data_type=CRMCustomFieldDataType.SELECT,
                    options={
                        "values": [
                            {"value": "legal", "active": True},
                            {"value": "removed", "active": False},
                        ]
                    },
                )
            ]
        )
    )
    service = DealCustomFieldWriteService(session=session, organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.validate_custom_properties({"procurement_stage": "removed"})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid option for custom field 'procurement_stage'"


def test_deal_custom_property_coercion_parses_percentage_as_number() -> None:
    service = DealCustomFieldWriteService(session=AsyncMock(), organisation_id=uuid4())

    value = service.coerce_custom_value(
        key="win_likelihood",
        value="42.5%",
        data_type=CRMCustomFieldDataType.PERCENTAGE,
    )

    assert value == 42.5


@pytest.mark.parametrize(
    "data_type",
    [
        CRMCustomFieldDataType.INTEGER,
        CRMCustomFieldDataType.NUMBER,
        CRMCustomFieldDataType.CURRENCY,
        CRMCustomFieldDataType.PERCENTAGE,
    ],
)
def test_deal_custom_property_numeric_coercion_rejects_boolean_values(
    data_type: CRMCustomFieldDataType,
) -> None:
    service = DealCustomFieldWriteService(session=AsyncMock(), organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError):
        service.coerce_custom_value(key="budget", value=True, data_type=data_type)


@pytest.mark.parametrize("value", [1.2, "1.2"])
def test_deal_custom_property_integer_coercion_rejects_fractional_values(value: object) -> None:
    service = DealCustomFieldWriteService(session=AsyncMock(), organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError):
        service.coerce_custom_value(
            key="seat_count",
            value=value,
            data_type=CRMCustomFieldDataType.INTEGER,
        )


@pytest.mark.parametrize("value", [-1, "101%"])
def test_deal_custom_property_percentage_coercion_rejects_out_of_range_values(
    value: object,
) -> None:
    service = DealCustomFieldWriteService(session=AsyncMock(), organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError):
        service.coerce_custom_value(
            key="win_likelihood",
            value=value,
            data_type=CRMCustomFieldDataType.PERCENTAGE,
        )


@pytest.mark.parametrize("value", [2, -1, 0.5])
def test_deal_custom_property_boolean_coercion_rejects_non_binary_numbers(
    value: object,
) -> None:
    service = DealCustomFieldWriteService(session=AsyncMock(), organisation_id=uuid4())

    with pytest.raises(DealWriteServiceError):
        service.coerce_custom_value(
            key="executive_sponsor",
            value=value,
            data_type=CRMCustomFieldDataType.BOOLEAN,
        )


@pytest.mark.asyncio
async def test_deal_schema_custom_column_keys_filter_internal_source() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )

    service = DealSchemaService(session=session, organisation_id=uuid4())
    await service.active_internal_custom_field_keys()

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text


@pytest.mark.asyncio
async def test_custom_field_write_service_reorders_complete_field_list() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    loaded_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    first_field = _custom_field_row(field_id=uuid4(), updated_at=loaded_at, display_order=1)
    second_field = _custom_field_row(field_id=uuid4(), updated_at=loaded_at, display_order=2)
    third_field = _custom_field_row(field_id=uuid4(), updated_at=loaded_at, display_order=3)
    service._load_reorderable_fields = AsyncMock(  # type: ignore[attr-defined]
        return_value=[first_field, second_field, third_field]
    )

    reordered = await service.reorder_fields(
        ReorderCustomFieldsRequest(
            entity=CRMCustomFieldEntity.DEAL,
            field_ids=[third_field.field_id, first_field.field_id, second_field.field_id],
            if_updated_at=loaded_at,
        )
    )

    assert [field.field_id for field in reordered] == [
        third_field.field_id,
        first_field.field_id,
        second_field.field_id,
    ]
    assert [field.display_order for field in reordered] == [1, 2, 3]
    assert session.flushed >= 1
    assert session.committed == 1


@pytest.mark.asyncio
async def test_custom_field_write_service_reorder_rejects_stale_version() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    stale_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    changed_field = _custom_field_row(
        field_id=uuid4(),
        updated_at=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        display_order=1,
    )
    service._load_reorderable_fields = AsyncMock(return_value=[changed_field])  # type: ignore[attr-defined]

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.reorder_fields(
            ReorderCustomFieldsRequest(
                entity=CRMCustomFieldEntity.DEAL,
                field_ids=[changed_field.field_id],
                if_updated_at=stale_at,
            )
        )

    assert exc_info.value.status_code == 409
    assert "Custom columns changed since they were loaded" in exc_info.value.message
    assert session.committed == 0


@pytest.mark.asyncio
async def test_custom_field_write_service_reorder_rejects_partial_or_foreign_ids() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    loaded_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    first_field = _custom_field_row(field_id=uuid4(), updated_at=loaded_at, display_order=1)
    second_field = _custom_field_row(field_id=uuid4(), updated_at=loaded_at, display_order=2)
    service._load_reorderable_fields = AsyncMock(  # type: ignore[attr-defined]
        return_value=[first_field, second_field]
    )

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.reorder_fields(
            ReorderCustomFieldsRequest(
                entity=CRMCustomFieldEntity.DEAL,
                field_ids=[first_field.field_id, uuid4()],
                if_updated_at=loaded_at,
            )
        )

    assert exc_info.value.status_code == 409
    assert "include every non-deleted custom column" in exc_info.value.message
    assert [first_field.display_order, second_field.display_order] == [1, 2]
    assert session.committed == 0


@pytest.mark.asyncio
async def test_custom_field_write_service_reorder_rejects_duplicate_ids() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    field_id = uuid4()

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.reorder_fields(
            ReorderCustomFieldsRequest(
                entity=CRMCustomFieldEntity.DEAL,
                field_ids=[field_id, field_id],
                if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Duplicate custom column ids are not allowed"
    assert session.committed == 0


@pytest.mark.asyncio
async def test_custom_field_write_service_update_rejects_stale_version_precondition() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    field = SimpleNamespace(
        field_id=uuid4(),
        key="priority",
        label="Priority",
        entity_type=CRMCustomFieldEntity.DEAL,
        data_type=CRMCustomFieldDataType.TEXT,
        updated_at=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        options={},
        validation={},
        formula=None,
    )
    service._load_field = AsyncMock(return_value=field)  # type: ignore[method-assign]
    service._validate_definition = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.update_field(
            field.field_id,
            UpdateCustomFieldRequest(
                label="Deal Priority",
                if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            ),
        )

    assert exc_info.value.status_code == 409
    assert "Custom column changed since it was loaded" in exc_info.value.message
    assert field.label == "Priority"
    service._validate_definition.assert_not_called()
    assert session.committed == 0


@pytest.mark.asyncio
async def test_custom_field_write_service_update_requires_version_precondition() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    field = SimpleNamespace(
        field_id=uuid4(),
        key="priority",
        label="Priority",
        entity_type=CRMCustomFieldEntity.DEAL,
        data_type=CRMCustomFieldDataType.TEXT,
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        options={},
        validation={},
        formula=None,
    )
    service._load_field = AsyncMock(return_value=field)  # type: ignore[method-assign]
    service._validate_definition = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.update_field(
            field.field_id,
            UpdateCustomFieldRequest.model_construct(label="Deal Priority", if_updated_at=None),
        )

    assert exc_info.value.status_code == 400
    assert "if_updated_at is required" in exc_info.value.message
    assert field.label == "Priority"
    service._validate_definition.assert_not_called()
    assert session.committed == 0


@pytest.mark.asyncio
async def test_custom_field_create_records_settings_mutation_event() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    service._key_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]

    field = await service.create_field(CreateCustomFieldRequest(key="priority", label="Priority"))

    events = _mutation_events(session)
    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "custom_field"
    assert event.entity_id == field.field_id
    assert event.actor_user_id == service.user_id
    assert event.source == "api"
    assert event.operation == "custom_field.create"
    assert event.changed_fields["label"] == {"old": None, "new": "Priority"}


@pytest.mark.asyncio
async def test_custom_field_update_records_archive_settings_mutation_event() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    field = SimpleNamespace(
        field_id=uuid4(),
        key="priority",
        label="Priority",
        entity_type=CRMCustomFieldEntity.DEAL,
        data_type=CRMCustomFieldDataType.TEXT,
        required=False,
        is_active=True,
        display_order=1,
        description=None,
        options={},
        validation={},
        formula=None,
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    service._load_field = AsyncMock(return_value=field)  # type: ignore[method-assign]
    service._validate_definition = AsyncMock()  # type: ignore[method-assign]

    await service.update_field(
        field.field_id,
        UpdateCustomFieldRequest(
            is_active=False,
            if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        ),
    )

    event = _mutation_events(session)[0]
    assert event.operation == "custom_field.update"
    assert event.changed_fields["is_active"] == {"old": True, "new": False}
    assert event.semantic_event_types == ["crm.settings.custom_field.updated"]


@pytest.mark.asyncio
async def test_custom_field_restore_reactivates_archived_field_and_records_event() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    field = SimpleNamespace(
        field_id=uuid4(),
        key="priority",
        label="Priority",
        entity_type=CRMCustomFieldEntity.DEAL,
        data_type=CRMCustomFieldDataType.TEXT,
        required=False,
        is_predefined=False,
        is_active=False,
        display_order=1,
        description=None,
        options={},
        validation={},
        formula=None,
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    service._load_field = AsyncMock(return_value=field)  # type: ignore[method-assign]
    service._validate_definition = AsyncMock()  # type: ignore[method-assign]

    restored = await service.restore_field(
        field.field_id,
        if_updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )

    assert restored.is_active is True
    event = _mutation_events(session)[0]
    assert event.operation == "custom_field.restore"
    assert event.changed_fields["is_active"] == {"old": False, "new": True}


@pytest.mark.asyncio
async def test_deal_schema_custom_column_definitions_filter_internal_source() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )

    service = DealSchemaService(session=session, organisation_id=uuid4())
    await service.deal_detail_custom_column_definitions(deal_currency_code=None)

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text


def test_custom_field_response_preserves_option_metadata_for_future_option_ids() -> None:
    field = type(
        "Field",
        (),
        {
            "field_id": uuid4(),
            "entity_type": CRMCustomFieldEntity.DEAL,
            "key": "procurement_stage",
            "label": "Procurement stage",
            "data_type": CRMCustomFieldDataType.SELECT,
            "required": False,
            "is_predefined": False,
            "is_active": True,
            "display_order": None,
            "description": None,
            "options": {"values": [{"id": "opt_legal", "value": "legal", "label": "Legal"}]},
            "validation": {},
            "formula": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
    )()

    item = _to_response_item(field)

    assert item.options == {"values": [{"id": "opt_legal", "value": "legal", "label": "Legal"}]}


def test_custom_field_response_includes_usage_count() -> None:
    field = type(
        "Field",
        (),
        {
            "field_id": uuid4(),
            "entity_type": CRMCustomFieldEntity.DEAL,
            "key": "procurement_stage",
            "label": "Procurement stage",
            "data_type": CRMCustomFieldDataType.SELECT,
            "required": False,
            "is_predefined": False,
            "is_active": True,
            "display_order": None,
            "description": None,
            "options": {},
            "validation": {},
            "formula": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
    )()

    assert _to_response_item(field).usage_count is None
    assert _to_response_item(field, usage_count=0).usage_count == 0
    assert _to_response_item(field, usage_count=2).usage_count == 2


def test_custom_field_response_includes_last_changed_metadata() -> None:
    actor_id = uuid4()
    field = type(
        "Field",
        (),
        {
            "field_id": uuid4(),
            "entity_type": CRMCustomFieldEntity.DEAL,
            "key": "procurement_stage",
            "label": "Procurement stage",
            "data_type": CRMCustomFieldDataType.SELECT,
            "required": False,
            "is_predefined": False,
            "is_active": True,
            "display_order": None,
            "description": None,
            "options": {},
            "validation": {},
            "formula": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
    )()
    event = SimpleNamespace(
        occurred_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
        actor_user_id=actor_id,
        source="api",
        event_type="crm.settings.custom_field.updated",
    )

    item = _to_response_item(field, last_changed_event=event)

    assert item.last_changed is not None
    assert item.last_changed.changed_at == event.occurred_at
    assert item.last_changed.actor_user_id == actor_id
    assert item.last_changed.source == "api"
    assert item.last_changed.event_type == "crm.settings.custom_field.updated"


@pytest.mark.asyncio
async def test_custom_field_write_service_counts_deal_usage_by_field_key() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"all": lambda self: [("priority", 1), ("risk", 3)]})()
    )
    service = CustomFieldWriteService(
        session=session,
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    counts = await service.count_deal_usage_by_field_key(["priority", "risk", "unused"])

    assert counts == {"priority": 1, "risk": 3}
    stmt_text = str(session.execute.await_args.args[0])
    assert "crm_deals.properties ? crm_custom_field_definitions.key" in stmt_text
    assert "GROUP BY crm_custom_field_definitions.key" in stmt_text


@pytest.mark.asyncio
async def test_custom_field_write_service_clears_values_for_archived_field() -> None:
    session = _CustomFieldSession()
    session.execute = AsyncMock()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    field = SimpleNamespace(key="priority", is_active=False, is_predefined=False)
    service._load_field = AsyncMock(return_value=field)  # type: ignore[method-assign]
    service._count_deals_using_field = AsyncMock(return_value=2)  # type: ignore[method-assign]

    cleared_count = await service.clear_field_values(uuid4())

    assert cleared_count == 2
    assert session.execute.await_count == 1
    assert "crm_deals.properties - :properties_1" in str(session.execute.await_args.args[0])
    assert session.committed == 1


@pytest.mark.asyncio
async def test_custom_field_write_service_rejects_clearing_active_field_values() -> None:
    session = _CustomFieldSession()
    session.execute = AsyncMock()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    field = SimpleNamespace(key="priority", is_active=True, is_predefined=False)
    service._load_field = AsyncMock(return_value=field)  # type: ignore[method-assign]

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.clear_field_values(uuid4())

    assert exc_info.value.status_code == 409
    assert "Archive the custom column before clearing its values" in exc_info.value.message
    session.execute.assert_not_awaited()
    assert session.committed == 0


@pytest.mark.asyncio
async def test_custom_field_write_service_rejects_duplicate_select_option_values() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    service._key_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.create_field(
            CreateCustomFieldRequest(
                key="priority",
                label="Priority",
                data_type=CRMCustomFieldDataType.SELECT,
                options={"values": ["High", {"value": " High "}]},
            )
        )

    assert exc_info.value.status_code == 400
    assert "option values must be unique" in exc_info.value.message
    assert session.committed == 0


@pytest.mark.asyncio
async def test_custom_field_write_service_rejects_empty_select_option_values() -> None:
    session = _CustomFieldSession()
    service = CustomFieldWriteService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
        user_id=uuid4(),
    )
    service._key_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(CustomFieldWriteServiceError) as exc_info:
        await service.create_field(
            CreateCustomFieldRequest(
                key="priority",
                label="Priority",
                data_type=CRMCustomFieldDataType.MULTISELECT,
                options={"values": [{"label": "No value"}]},
            )
        )

    assert exc_info.value.status_code == 400
    assert "option values cannot be empty" in exc_info.value.message
    assert session.committed == 0
