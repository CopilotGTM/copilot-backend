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
