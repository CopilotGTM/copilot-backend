from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.api.routers.crm.deals_handler import (
    _get_active_deal_custom_field_definitions,
    _get_active_deal_custom_field_keys,
)
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldEntity
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
