from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.integrations.crm.records.salesforce.users import user_writer
from src.service.integrations.crm.records.salesforce.users.user_sync_service import (
    _backfill_owner_tables,
)
from src.service.integrations.crm.records.salesforce.users.user_types import (
    NormalizedSalesforceUser,
    UserMatchResult,
)
from src.service.integrations.crm.records.salesforce.users.user_writer import (
    SalesforceUserWriter,
)


def _normalized_user(
    *,
    external_id: str,
    email: str | None,
    is_active: bool,
) -> NormalizedSalesforceUser:
    return NormalizedSalesforceUser(
        external_id=external_id,
        email=email,
        first_name="Alex",
        last_name="Morgan",
        full_name="Alex Morgan",
        user_role="Sales User",
        is_active=is_active,
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={"Id": external_id},
    )


@pytest.mark.asyncio
async def test_writer_mints_uuid_for_unmatched_and_maps_duplicate_public_match() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    matched_user_id = uuid4()

    existing = CRMUser(
        user_id=matched_user_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        email="owner@acme.com",
        full_name="Alex Morgan",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={"005A": existing})  # type: ignore[method-assign]
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            UserMatchResult(
                user=_normalized_user(
                    external_id="005A",
                    email="owner@acme.com",
                    is_active=True,
                ),
                matched_app_user_id=matched_user_id,
            ),
            UserMatchResult(
                user=_normalized_user(
                    external_id="005B",
                    email="owner@acme.com",
                    is_active=True,
                ),
                matched_app_user_id=matched_user_id,
            ),
            UserMatchResult(
                user=_normalized_user(
                    external_id="005C",
                    email=None,
                    is_active=False,
                ),
                matched_app_user_id=None,
            ),
        ],
        all_external_ids=["005A", "005B", "005C"],
    )

    assert result.users_updated == 2
    assert result.skipped_dup_email == 0
    assert result.users_created == 1
    assert result.users_minted_uuid == 1
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert len(created_rows) == 1
    assert not hasattr(created_rows[0], "external_id")
    assert created_rows[0].is_active is False
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    mapped_external_ids = [row.external_id for row in mapped_rows]
    assert mapped_external_ids == ["005A", "005B", "005C"]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.USER
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.USER
    assert mapped_rows[0].local_object_id == existing.user_id
    assert mapped_rows[1].local_object_id == existing.user_id


@pytest.mark.asyncio
async def test_writer_maps_duplicate_email_provider_users_to_one_crm_user() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]
    writer._load_existing_rows_by_user_ids = AsyncMock(return_value={})  # type: ignore[attr-defined]
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            UserMatchResult(
                user=_normalized_user(
                    external_id="005A",
                    email="Owner@Acme.com",
                    is_active=True,
                ),
                matched_app_user_id=None,
            ),
            UserMatchResult(
                user=_normalized_user(
                    external_id="005B",
                    email="owner@acme.com",
                    is_active=True,
                ),
                matched_app_user_id=None,
            ),
        ],
        all_external_ids=["005A", "005B"],
    )

    assert result.users_created == 1
    assert result.users_updated == 1
    assert result.skipped_dup_email == 0
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert len(created_rows) == 1
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert [row.external_id for row in mapped_rows] == ["005A", "005B"]
    assert {row.local_object_id for row in mapped_rows} == {created_rows[0].user_id}


@pytest.mark.asyncio
async def test_writer_deactivates_missing_rows_but_keeps_present_inactive_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing_inactive = CRMUser(
        user_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        email="former@acme.com",
        full_name="Former Owner",
        is_active=False,
    )
    stale_missing = CRMUser(
        user_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        email="stale@acme.com",
        full_name="Stale Owner",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"005A": existing_inactive, "005B": stale_missing}
    )
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            UserMatchResult(
                user=_normalized_user(
                    external_id="005A",
                    email="former@acme.com",
                    is_active=False,
                ),
                matched_app_user_id=None,
            )
        ],
        all_external_ids=["005A"],
    )

    assert result.users_updated == 1
    assert result.users_reactivated == 0
    assert result.users_deactivated == 1
    assert existing_inactive.is_active is False
    assert stale_missing.is_active is False


@pytest.mark.asyncio
async def test_writer_can_skip_missing_deactivation_for_single_user_cdc() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    stale_missing = CRMUser(
        user_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        email="stale@acme.com",
        full_name="Stale Owner",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={"005B": stale_missing})  # type: ignore[method-assign]
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            UserMatchResult(
                user=_normalized_user(
                    external_id="005A",
                    email="owner@acme.com",
                    is_active=True,
                ),
                matched_app_user_id=None,
            )
        ],
        all_external_ids=["005A"],
        deactivate_missing=False,
    )

    assert result.users_created == 1
    assert result.users_deactivated == 0
    assert stale_missing.is_active is True


@pytest.mark.asyncio
async def test_writer_reuses_existing_local_crm_user_when_provider_mapping_is_missing() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    app_user_id = uuid4()
    existing = CRMUser(
        user_id=app_user_id,
        organisation_id=organisation_id,
        integration_id=uuid4(),
        email="owner@acme.com",
        full_name="Existing App User",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]
    writer._load_existing_rows_by_user_ids = AsyncMock(  # type: ignore[attr-defined]
        return_value={app_user_id: existing}
    )
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            UserMatchResult(
                user=_normalized_user(
                    external_id="005A",
                    email="owner@acme.com",
                    is_active=True,
                ),
                matched_app_user_id=app_user_id,
            )
        ],
        all_external_ids=["005A"],
    )

    assert result.users_created == 0
    assert result.users_updated == 1
    assert result.users_matched_public == 1
    assert existing.full_name == "Alex Morgan"
    session.add.assert_not_called()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert len(mapped_rows) == 1
    assert mapped_rows[0].external_id == "005A"
    assert mapped_rows[0].local_object_id == app_user_id


@pytest.mark.asyncio
async def test_owner_table_backfill_is_disabled_after_provider_column_removal() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    owner_id = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=0))

    result = await _backfill_owner_tables(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        owner_mapping={"005A": (owner_id, "Alex Morgan", None, "owner@acme.com")},
    )

    assert result == {
        "crm_companies.owner_id": 0,
        "crm_contacts.owner_id": 0,
        "crm_activities.owner_id": 0,
    }
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_load_existing_rows_chunks_local_id_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    rows = [
        CRMUser(
            user_id=uuid4(),
            organisation_id=organisation_id,
            integration_id=integration_id,
            email=f"owner{index}@acme.com",
            full_name=f"Owner {index}",
            is_active=True,
        )
        for index in range(3)
    ]

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows[:2])),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows[2:])),
        ]
    )

    writer = SalesforceUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer.source_mappings = MagicMock()
    writer.source_mappings.load_local_id_map = AsyncMock(
        return_value={f"005{index}": row.user_id for index, row in enumerate(rows)}
    )
    monkeypatch.setattr(user_writer, "LOCAL_ID_QUERY_CHUNK_SIZE", 2)

    existing = await writer._load_existing_rows()

    assert existing == {f"005{index}": row for index, row in enumerate(rows)}
    assert session.execute.await_count == 2
