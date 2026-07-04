from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.integrations.crm.records.hubspot.users.user_types import (
    NormalizedHubSpotOwner,
    OwnerMatchResult,
)
from src.service.integrations.crm.records.hubspot.users.user_writer import HubSpotUserWriter


def _normalized_owner(
    *, external_id: str, email: str | None = "owner@example.com"
) -> NormalizedHubSpotOwner:
    return NormalizedHubSpotOwner(
        external_id=external_id,
        email=email,
        first_name="Alex",
        last_name="Morgan",
        full_name="Alex Morgan",
        user_role="Sales",
        primary_team_id=None,
        team_ids=(),
        source_created_at=None,
        source_modified_at=None,
        properties={},
        data_payload={"id": external_id},
    )


@pytest.mark.asyncio
async def test_writer_upserts_user_source_mapping_and_marks_missing_deleted() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMUser(
        user_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="owner-1",
        email="owner@example.com",
        full_name="Old Owner",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={"owner-1": existing})  # type: ignore[method-assign]
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            OwnerMatchResult(
                owner=_normalized_owner(external_id="owner-1"),
                matched_app_user_id=existing.user_id,
            )
        ],
        all_external_ids=(external_id for external_id in ["owner-1"]),
    )

    assert result.users_updated == 1
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["external_object_type"] == (
        CRMSourceObjectType.USER
    )
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_id"] == (
        existing.user_id
    )
    mapping_helper.mark_missing_deleted.assert_awaited_once_with(
        object_type=CRMSourceObjectType.USER,
        seen_external_ids={"owner-1"},
    )


@pytest.mark.asyncio
async def test_writer_reactivates_soft_deleted_mapped_user() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMUser(
        user_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="owner-1",
        email="owner@example.com",
        full_name="Deleted Owner",
        is_active=False,
        is_deleted=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={"owner-1": existing})  # type: ignore[method-assign]
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            OwnerMatchResult(
                owner=_normalized_owner(external_id="owner-1"),
                matched_app_user_id=existing.user_id,
            )
        ],
        all_external_ids=["owner-1"],
    )

    assert result.users_reactivated == 1
    assert existing.is_deleted is False
    assert existing.is_active is True


@pytest.mark.asyncio
async def test_writer_reuses_existing_local_crm_user_when_provider_mapping_is_missing() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    app_user_id = uuid4()
    existing = CRMUser(
        user_id=app_user_id,
        organisation_id=organisation_id,
        integration_id=uuid4(),
        email="owner@example.com",
        full_name="Existing App User",
        is_active=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]
    writer._load_existing_rows_by_user_ids = AsyncMock(  # type: ignore[attr-defined]
        return_value={app_user_id: existing}
    )
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            OwnerMatchResult(
                owner=_normalized_owner(external_id="owner-1"),
                matched_app_user_id=app_user_id,
            )
        ],
        all_external_ids=["owner-1"],
    )

    assert result.users_created == 0
    assert result.users_updated == 1
    assert result.users_matched_public == 1
    assert existing.full_name == "Alex Morgan"
    session.add.assert_not_called()
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["local_object_id"] == (
        app_user_id
    )


@pytest.mark.asyncio
async def test_writer_maps_duplicate_email_owners_to_one_crm_user() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotUserWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]
    writer._load_existing_rows_by_user_ids = AsyncMock(return_value={})  # type: ignore[attr-defined]
    writer._load_existing_rows_by_email = AsyncMock(return_value={})  # type: ignore[attr-defined]

    result = await writer.apply(
        matches=[
            OwnerMatchResult(
                owner=_normalized_owner(external_id="owner-1", email="owner@example.com"),
                matched_app_user_id=None,
            ),
            OwnerMatchResult(
                owner=_normalized_owner(external_id="owner-2", email="owner@example.com"),
                matched_app_user_id=None,
            ),
        ],
        all_external_ids=["owner-1", "owner-2"],
    )

    assert result.users_created == 1
    assert result.users_updated == 1
    assert result.skipped_dup_email == 0
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert len(created_rows) == 1
    mapped_user_ids = [
        call.kwargs["local_object_id"]
        for call in mapping_helper.upsert_integration_mapping.await_args_list
    ]
    assert mapped_user_ids == [created_rows[0].user_id, created_rows[0].user_id]
