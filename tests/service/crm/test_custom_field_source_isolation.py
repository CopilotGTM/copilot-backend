from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.api.routers.crm.deals_handler import (
    _get_active_deal_custom_field_definitions,
    _get_active_deal_custom_field_keys,
)
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.service.crm.deals.custom_field_service import DealCustomFieldWriteService
from src.service.crm.internal.custom_field_service import CustomFieldService
from src.service.crm.internal.custom_field_write_service import (
    CustomFieldWriteService,
    CustomFieldWriteServiceError,
)
from src.service.crm.internal.deal_write_service import DealWriteService, DealWriteServiceError


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
async def test_deal_custom_column_keys_filter_internal_source() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=type("R", (), {"all": lambda self: []})())

    await _get_active_deal_custom_field_keys(session, uuid4())

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text


@pytest.mark.asyncio
async def test_deal_custom_column_definitions_filter_internal_source() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=type("R", (), {"scalars": lambda self: self, "all": lambda self: []})()
    )

    await _get_active_deal_custom_field_definitions(session, uuid4())

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "field_source" in stmt_text
    assert "internal" in stmt_text
