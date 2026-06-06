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
