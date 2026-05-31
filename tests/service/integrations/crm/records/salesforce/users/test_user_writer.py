from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
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
async def test_writer_mints_uuid_for_unmatched_and_skips_duplicate_public_match() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    matched_user_id = uuid4()

    existing = CRMUser(
        user_id=matched_user_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="005A",
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
    writer._load_existing_rows = AsyncMock(return_value={"005A": existing})  # type: ignore[method-assign]

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

    assert result.users_updated == 1
    assert result.skipped_dup_email == 1
    assert result.users_created == 1
    assert result.users_minted_uuid == 1
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert len(created_rows) == 1
    assert created_rows[0].external_id == "005C"
    assert created_rows[0].is_active is False


@pytest.mark.asyncio
async def test_writer_deactivates_missing_rows_but_keeps_present_inactive_rows() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing_inactive = CRMUser(
        user_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="005A",
        email="former@acme.com",
        full_name="Former Owner",
        is_active=False,
    )
    stale_missing = CRMUser(
        user_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="005B",
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
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"005A": existing_inactive, "005B": stale_missing}
    )

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
        external_id="005B",
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
    writer._load_existing_rows = AsyncMock(return_value={"005B": stale_missing})  # type: ignore[method-assign]

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
async def test_owner_activity_backfill_is_scoped_to_salesforce_tasks_and_notes() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    owner_id = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=0))

    await _backfill_owner_tables(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        owner_mapping={"005A": (owner_id, "Alex Morgan", None, "owner@acme.com")},
    )

    activity_statement = session.execute.call_args_list[-1].args[0]
    sql_text = str(activity_statement)
    assert "UPDATE crm_activities SET" in sql_text
    assert "activity_type IN" in sql_text
    assert "activity_types" in sql_text
    assert session.execute.call_args_list[-1].args[1]["activity_types"] == (
        "TASK",
        "TODO",
        "NOTE",
    )
