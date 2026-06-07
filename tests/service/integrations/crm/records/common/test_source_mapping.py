from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.crm import (
    CRMSourceKind,
    CRMSourceObjectMapping,
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.common.source_mapping import (
    CRMSourceMappingHelper,
    IntegrationMappingUpsert,
)


@pytest.mark.asyncio
async def test_load_local_id_map_normalizes_provider_and_filters_active_mappings() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    user_id = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: [("005A", user_id)]))

    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider=" Salesforce ",
    )

    result = await helper.load_local_id_map(
        object_type=CRMSourceObjectType.USER,
        external_ids={"005A"},
    )

    statement = session.execute.call_args.args[0]
    sql_text = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert result == {"005A": user_id}
    assert "crm_source_object_mappings.provider = 'salesforce'" in sql_text
    assert "crm_source_object_mappings.is_deleted = false" in sql_text


@pytest.mark.asyncio
async def test_upsert_integration_mapping_reactivates_existing_mapping() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    old_user_id = uuid4()
    new_user_id = uuid4()
    synced_at = datetime.now(UTC)
    existing = CRMSourceObjectMapping(
        organisation_id=organisation_id,
        source_kind=CRMSourceKind.INTEGRATION,
        integration_id=integration_id,
        provider="salesforce",
        external_object_type=CRMSourceObjectType.USER,
        external_id="005A",
        local_object_type=CRMSourceObjectType.USER,
        local_object_id=old_user_id,
        data_payload={"Id": "old"},
        is_deleted=True,
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing))
    session.add = MagicMock()

    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )

    mapping = await helper.upsert_integration_mapping(
        external_object_type=CRMSourceObjectType.USER,
        external_id="005A",
        local_object_type=CRMSourceObjectType.USER,
        local_object_id=new_user_id,
        data_payload={"Id": "005A"},
        source_created_at=None,
        source_modified_at=synced_at,
        synced_at=synced_at,
    )

    assert mapping is existing
    assert existing.local_object_id == new_user_id
    assert existing.data_payload == {"Id": "005A"}
    assert existing.source_modified_at == synced_at
    assert existing.last_seen_at == synced_at
    assert existing.is_deleted is False
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_integration_mapping_uses_database_conflict_update_for_new_mapping() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_user_id = uuid4()
    synced_at = datetime.now(UTC)
    created = CRMSourceObjectMapping(
        organisation_id=organisation_id,
        source_kind=CRMSourceKind.INTEGRATION,
        integration_id=integration_id,
        provider="salesforce",
        external_object_type=CRMSourceObjectType.USER,
        external_id="005A",
        local_object_type=CRMSourceObjectType.USER,
        local_object_id=local_user_id,
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: None),
            SimpleNamespace(scalar_one=lambda: created),
        ]
    )

    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )

    mapping = await helper.upsert_integration_mapping(
        external_object_type=CRMSourceObjectType.USER,
        external_id="005A",
        local_object_type=CRMSourceObjectType.USER,
        local_object_id=local_user_id,
        data_payload={"Id": "005A"},
        source_created_at=None,
        source_modified_at=synced_at,
        synced_at=synced_at,
    )

    upsert_statement = session.execute.call_args_list[1].args[0]
    sql_text = str(upsert_statement.compile(dialect=postgresql.dialect()))
    assert mapping is created
    assert "ON CONFLICT" in sql_text
    assert "WHERE source_kind = 'integration'" in sql_text
    assert "DO UPDATE" in sql_text


@pytest.mark.asyncio
async def test_bulk_upsert_integration_mappings_uses_partial_index_conflict_update() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_company_id = uuid4()
    synced_at = datetime.now(UTC)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider=" Salesforce ",
    )

    row_count = await helper.bulk_upsert_integration_mappings(
        [
            IntegrationMappingUpsert(
                external_object_type=CRMSourceObjectType.COMPANY,
                external_id=" 001A ",
                local_object_type=CRMSourceObjectType.COMPANY,
                local_object_id=local_company_id,
                data_payload={"Id": "001A"},
                source_created_at=None,
                source_modified_at=synced_at,
                synced_at=synced_at,
            )
        ]
    )

    statement = session.execute.await_args.args[0]
    sql_text = str(statement.compile(dialect=postgresql.dialect()))
    assert row_count == 1
    assert "ON CONFLICT" in sql_text
    assert (
        "organisation_id, integration_id, provider, external_object_type, external_id" in sql_text
    )
    assert "WHERE source_kind = 'integration'" in sql_text
    assert "DO UPDATE" in sql_text
    assert "updated_at = now()" in sql_text


@pytest.mark.asyncio
async def test_bulk_upsert_integration_mappings_updates_metadata_only_when_supplied() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    synced_at = datetime.now(UTC)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )

    await helper.bulk_upsert_integration_mappings(
        [
            IntegrationMappingUpsert(
                external_object_type=CRMSourceObjectType.DEAL,
                external_id="006A",
                local_object_type=CRMSourceObjectType.DEAL,
                local_object_id=uuid4(),
                data_payload={"Id": "006A"},
                source_created_at=None,
                source_modified_at=synced_at,
                synced_at=synced_at,
                mapping_metadata={"associations_payload": {"contact_count": 1}},
            ),
            IntegrationMappingUpsert(
                external_object_type=CRMSourceObjectType.CONTACT,
                external_id="003A",
                local_object_type=CRMSourceObjectType.CONTACT,
                local_object_id=uuid4(),
                data_payload={"Id": "003A"},
                source_created_at=None,
                source_modified_at=synced_at,
                synced_at=synced_at,
            ),
        ]
    )

    sql_texts = [
        str(call.args[0].compile(dialect=postgresql.dialect()))
        for call in session.execute.await_args_list
    ]
    assert len(sql_texts) == 2
    assert any("mapping_metadata = excluded.mapping_metadata" in sql for sql in sql_texts)
    assert any("mapping_metadata = excluded.mapping_metadata" not in sql for sql in sql_texts)


@pytest.mark.asyncio
async def test_bulk_upsert_integration_mappings_dedupes_external_ids() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    first_company_id = uuid4()
    second_company_id = uuid4()
    synced_at = datetime.now(UTC)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )

    row_count = await helper.bulk_upsert_integration_mappings(
        [
            IntegrationMappingUpsert(
                external_object_type=CRMSourceObjectType.COMPANY,
                external_id="001A",
                local_object_type=CRMSourceObjectType.COMPANY,
                local_object_id=first_company_id,
                data_payload={"Id": "old"},
                source_created_at=None,
                source_modified_at=None,
                synced_at=synced_at,
            ),
            IntegrationMappingUpsert(
                external_object_type=CRMSourceObjectType.COMPANY,
                external_id="001A",
                local_object_type=CRMSourceObjectType.COMPANY,
                local_object_id=second_company_id,
                data_payload={"Id": "new"},
                source_created_at=None,
                source_modified_at=synced_at,
                synced_at=synced_at,
            ),
        ]
    )

    statement = session.execute.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    params = compiled.params
    local_id_params = [value for key, value in params.items() if key.startswith("local_object_id")]
    payload_params = [value for key, value in params.items() if key.startswith("data_payload")]
    assert row_count == 1
    assert local_id_params == [second_company_id]
    assert payload_params == [{"Id": "new"}]


@pytest.mark.asyncio
async def test_bulk_upsert_integration_mappings_skips_empty_input() -> None:
    helper = CRMSourceMappingHelper(
        session=AsyncMock(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
        provider="salesforce",
    )

    row_count = await helper.bulk_upsert_integration_mappings([])

    assert row_count == 0
    helper.session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_upsert_integration_mappings_splits_large_batches() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    synced_at = datetime.now(UTC)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )
    rows = [
        IntegrationMappingUpsert(
            external_object_type=CRMSourceObjectType.COMPANY,
            external_id=f"001{i}",
            local_object_type=CRMSourceObjectType.COMPANY,
            local_object_id=uuid4(),
            data_payload={"Id": f"001{i}"},
            source_created_at=None,
            source_modified_at=None,
            synced_at=synced_at,
        )
        for i in range(2_000)
    ]

    row_count = await helper.bulk_upsert_integration_mappings(rows)

    assert row_count == 2_000
    assert session.execute.await_count > 1


@pytest.mark.asyncio
async def test_mark_missing_deleted_deletes_only_unseen_active_mappings() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    seen_mapping = CRMSourceObjectMapping(
        organisation_id=organisation_id,
        source_kind=CRMSourceKind.INTEGRATION,
        integration_id=integration_id,
        provider="salesforce",
        external_object_type=CRMSourceObjectType.COMPANY,
        external_id="001A",
        local_object_type=CRMSourceObjectType.COMPANY,
        local_object_id=uuid4(),
    )
    stale_mapping = CRMSourceObjectMapping(
        organisation_id=organisation_id,
        source_kind=CRMSourceKind.INTEGRATION,
        integration_id=integration_id,
        provider="salesforce",
        external_object_type=CRMSourceObjectType.COMPANY,
        external_id="001B",
        local_object_type=CRMSourceObjectType.COMPANY,
        local_object_id=uuid4(),
    )
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = [seen_mapping, stale_mapping]
    session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalars))
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    )

    deleted = await helper.mark_missing_deleted(
        object_type=CRMSourceObjectType.COMPANY,
        seen_external_ids={"001A"},
    )

    assert deleted == 1
    assert seen_mapping.is_deleted is False
    assert stale_mapping.is_deleted is True


@pytest.mark.asyncio
async def test_mark_external_ids_deleted_returns_local_ids_and_tombstones_mappings() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_deal_id = uuid4()
    matching = CRMSourceObjectMapping(
        organisation_id=organisation_id,
        source_kind=CRMSourceKind.INTEGRATION,
        integration_id=integration_id,
        provider="hubspot",
        external_object_type=CRMSourceObjectType.DEAL,
        external_id="101",
        local_object_type=CRMSourceObjectType.DEAL,
        local_object_id=local_deal_id,
    )
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = [matching]
    session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalars))
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider=" HubSpot ",
    )

    deleted = await helper.mark_external_ids_deleted(
        object_type=CRMSourceObjectType.DEAL,
        external_ids=["101", "missing"],
    )

    statement = session.execute.await_args.args[0]
    sql_text = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert deleted == {"101": local_deal_id}
    assert matching.is_deleted is True
    assert "crm_source_object_mappings.provider = 'hubspot'" in sql_text
    assert "crm_source_object_mappings.is_deleted = false" in sql_text
