"""Regression tests for the deal schema metadata service."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.service.crm.schema.deal_schema_service import DealSchemaService


class _FieldRowsResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_FieldRowsResult":
        """Match SQLAlchemy's scalar result chain used by the service."""
        return self

    def all(self) -> list[object]:
        """Return the fake rows supplied by each schema service test."""
        return self._rows


def _field(
    key: str,
    *,
    source: CRMCustomFieldSource = CRMCustomFieldSource.INTERNAL,
    data_type: CRMCustomFieldDataType = CRMCustomFieldDataType.TEXT,
    required: bool = False,
    options: dict | None = None,
    provider_name: str | None = None,
    provider_field_key: str | None = None,
    display_order: int | None = None,
) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        field_id=uuid4(),
        entity_type=CRMCustomFieldEntity.DEAL,
        field_source=source,
        integration_id=uuid4() if source == CRMCustomFieldSource.PROVIDER else None,
        provider_name=provider_name,
        key=key,
        provider_field_key=provider_field_key,
        label=key.replace("_", " ").title(),
        data_type=data_type,
        required=required,
        is_predefined=False,
        is_provider_custom=source == CRMCustomFieldSource.PROVIDER,
        is_active=True,
        is_deleted=False,
        display_order=display_order,
        description=None,
        options=options or {},
        validation={},
        formula=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_deal_schema_combines_standard_internal_and_provider_fields() -> None:
    """Schema responses should combine standard, internal, and provider fields safely."""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _field(
                    "procurement_stage",
                    data_type=CRMCustomFieldDataType.SELECT,
                    required=True,
                    options={"values": [{"id": "legal", "value": "legal", "label": "Legal"}]},
                    display_order=1,
                ),
                _field(
                    "hubspot_score",
                    source=CRMCustomFieldSource.PROVIDER,
                    data_type=CRMCustomFieldDataType.NUMBER,
                    provider_name="hubspot",
                    provider_field_key="hs_deal_score",
                    display_order=2,
                ),
            ]
        )
    )
    service = DealSchemaService(session=session, organisation_id=uuid4())

    schema = await service.get_schema()

    fields = [item.model_dump(mode="json") for item in schema.fields]
    fields_by_key = {item["key"]: item for item in fields}

    assert schema.entity == "deal"
    assert fields[0]["key"] == "name"
    assert fields[0]["source"] == "standard"
    assert fields[0]["editable"] is True
    assert fields[0]["write_path"] == "name"
    assert fields[0]["internal_write"] is True

    procurement = fields_by_key["procurement_stage"]
    assert procurement["source"] == "internal"
    assert procurement["ownership"] == "internal"
    assert procurement["type"] == "select"
    assert procurement["required"] is True
    assert procurement["nullable"] is False
    assert procurement["editable"] is True
    assert procurement["read_only"] is False
    assert procurement["write_path"] == "custom_columns.procurement_stage"
    assert procurement["write_policy"] == "custom_columns"
    assert procurement["input_control"] == "select"
    assert procurement["internal_write"] is True
    assert procurement["internal_write_validation"] == {
        "required": True,
        "option_membership": True,
    }
    assert procurement["options"] == {
        "values": [{"id": "legal", "value": "legal", "label": "Legal"}]
    }

    provider = fields_by_key["hubspot_score"]
    assert provider["source"] == "provider"
    assert provider["ownership"] == "hubspot"
    assert provider["provider_field_key"] == "hs_deal_score"
    assert provider["editable"] is False
    assert provider["read_only"] is True
    assert provider["write_path"] is None
    assert provider["write_policy"] == "provider_read_only"
    assert (
        provider["read_only_reason"]
        == "Provider-backed fields are not writable through internal CRM writes."
    )
    assert provider["internal_write"] is False
    assert provider["internal_write_validation"] == {
        "required": False,
        "option_membership": False,
    }


@pytest.mark.asyncio
async def test_deal_schema_query_includes_active_non_deleted_deal_fields_from_all_sources() -> None:
    """The schema query should not filter out provider fields at the SQL boundary."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FieldRowsResult([]))
    service = DealSchemaService(session=session, organisation_id=uuid4())

    await service.get_schema()

    stmt = session.execute.await_args.args[0]
    stmt_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "entity_type = 'DEAL'" in stmt_text or "entity_type = 'deal'" in stmt_text
    assert "is_deleted IS false" in stmt_text
    assert "is_active IS true" in stmt_text
    assert "field_source" not in stmt_text.split("WHERE", 1)[1].split("ORDER BY", 1)[0]


@pytest.mark.asyncio
async def test_deal_schema_service_returns_internal_custom_field_keys_only() -> None:
    """Detail null-filling should use internal custom keys, not provider fields."""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _field("procurement_stage"),
                _field(
                    "hubspot_score",
                    source=CRMCustomFieldSource.PROVIDER,
                    provider_name="hubspot",
                    provider_field_key="hs_deal_score",
                ),
            ]
        )
    )
    service = DealSchemaService(session=session, organisation_id=uuid4())

    keys = await service.active_internal_custom_field_keys()

    assert keys == ["procurement_stage"]


@pytest.mark.parametrize(
    ("data_type", "edit_control", "editable"),
    [
        (CRMCustomFieldDataType.TEXT, "text", True),
        (CRMCustomFieldDataType.NUMBER, "number", True),
        (CRMCustomFieldDataType.CURRENCY, "currency", True),
        (CRMCustomFieldDataType.DATE, "date", True),
        (CRMCustomFieldDataType.DATETIME, "datetime", True),
        (CRMCustomFieldDataType.BOOLEAN, "boolean", True),
        (CRMCustomFieldDataType.SELECT, "select", True),
        (CRMCustomFieldDataType.MULTISELECT, "multiselect", True),
        (CRMCustomFieldDataType.FORMULA, None, False),
    ],
)
@pytest.mark.asyncio
async def test_deal_schema_service_builds_detail_custom_column_projection_for_supported_types(
    data_type: CRMCustomFieldDataType,
    edit_control: str | None,
    editable: bool,
) -> None:
    """Existing deal detail controls should render from the shared schema service."""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FieldRowsResult(
            [
                _field(
                    "typed_field",
                    data_type=data_type,
                    options={"values": [{"id": "opt_1", "value": "one"}]},
                    display_order=1,
                )
            ]
        )
    )
    service = DealSchemaService(session=session, organisation_id=uuid4())

    definitions = await service.deal_detail_custom_column_definitions(deal_currency_code="INR")

    assert len(definitions) == 1
    definition = definitions[0].model_dump(mode="json")
    assert definition["key"] == "typed_field"
    assert definition["data_type"] == data_type.value
    assert definition["editable"] is editable
    assert definition["edit_control"] == edit_control
    assert definition["patch_field"] == ("custom_columns.typed_field" if editable else None)
    assert definition["edit_policy"] == ("custom_columns" if editable else "formula")
    if data_type == CRMCustomFieldDataType.CURRENCY:
        assert definition["currency_code"] == "INR"
        assert definition["currency_symbol"] == "₹"
