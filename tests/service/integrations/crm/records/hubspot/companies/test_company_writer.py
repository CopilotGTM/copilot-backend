"""Regression coverage for HubSpot company writer webhook interactions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.hubspot.companies.company_types import (
    NormalizedHubSpotCompany,
)
from src.service.integrations.crm.records.hubspot.companies.company_writer import (
    HubSpotCompanyWriter,
)


def _normalized_company(
    *,
    external_id: str,
    owner_external_id: str | None = None,
    name: str = "Acme",
) -> NormalizedHubSpotCompany:
    return NormalizedHubSpotCompany(
        external_id=external_id,
        name=name,
        domain="acme.example",
        website="https://acme.example",
        description="Key account",
        industry="Software",
        company_type="Customer",
        lifecycle_stage="customer",
        owner_external_id=owner_external_id,
        number_of_employees=25,
        annual_revenue=1000.0,
        phone="123",
        city="Pune",
        state="MH",
        country="India",
        address="1 Main St",
        postal_code="411001",
        timezone=None,
        linkedin_url=None,
        source_created_at=None,
        source_modified_at=None,
        properties={"raw_properties": {}},
        data_payload={"id": external_id},
    )


def _mock_source_mappings(writer: HubSpotCompanyWriter) -> MagicMock:
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.mark_missing_deleted = AsyncMock()
    writer.source_mappings = mapping_helper
    return mapping_helper


@pytest.mark.asyncio
async def test_single_apply_does_not_deactivate_unrelated_companies() -> None:
    """Webhook company upserts must not treat one event as a full active snapshot."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Old Acme",
        is_active=True,
    )
    unrelated = CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="999",
        name="Other",
        is_active=True,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotCompanyWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"123": existing, "999": unrelated}
    )
    _mock_source_mappings(writer)

    result = await writer.apply_single(
        company=_normalized_company(external_id="123", name="New Acme"),
        owner_map={},
    )

    assert result.companies_updated == 1
    assert result.companies_deactivated == 0
    assert existing.name == "New Acme"
    assert unrelated.is_active is True
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_single_apply_reactivates_soft_deleted_company() -> None:
    """HubSpot upserts revive a local company tombstone instead of inserting a duplicate."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Deleted Acme",
        is_active=False,
        is_deleted=True,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotCompanyWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    mapping_helper = _mock_source_mappings(writer)

    result = await writer.apply_single(
        company=_normalized_company(external_id="123", name="Restored Acme"),
        owner_map={},
    )

    assert result.companies_created == 0
    assert result.companies_reactivated == 1
    assert existing.is_deleted is False
    assert existing.is_active is True
    assert existing.name == "Restored Acme"
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["external_object_type"] == (
        CRMSourceObjectType.COMPANY
    )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_single_apply_resolves_owner_and_counts_unresolved_owner() -> None:
    """Owner resolution uses crm_users mapping and leaves missing owners nullable."""
    organisation_id = uuid4()
    integration_id = uuid4()
    owner_id = uuid4()
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotCompanyWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    created = await writer.apply_single(
        company=_normalized_company(external_id="123", owner_external_id="owner-1"),
        owner_map={"owner-1": owner_id},
    )
    unresolved = await writer.apply_single(
        company=_normalized_company(external_id="456", owner_external_id="missing"),
        owner_map={},
    )

    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert created.companies_created == 1
    assert unresolved.unresolved_owner_link == 1
    assert created_rows[0].owner_id == owner_id
    assert created_rows[1].owner_id is None


@pytest.mark.asyncio
async def test_single_apply_skips_stale_active_company_without_overwriting_snapshot() -> None:
    """Older fetched company payloads must not overwrite newer webhook-written rows."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Webhook latest Acme",
        source_modified_at=datetime(2026, 5, 3, tzinfo=UTC),
        is_active=True,
        is_deleted=False,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotCompanyWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    result = await writer.apply_single(
        company=replace(
            _normalized_company(external_id="123", name="Older incremental Acme"),
            source_modified_at=datetime(2026, 5, 2, tzinfo=UTC),
        ),
        owner_map={},
    )

    assert result.companies_created == 0
    assert result.companies_updated == 0
    assert result.companies_skipped_stale == 1
    assert result.skipped_stale_external_ids == {"123"}
    assert existing.name == "Webhook latest Acme"
    assert existing.source_modified_at == datetime(2026, 5, 3, tzinfo=UTC)
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_single_apply_reactivates_soft_deleted_company_even_when_timestamp_matches() -> None:
    """Freshness skip must not block HubSpot restore/upsert reactivation."""
    organisation_id = uuid4()
    integration_id = uuid4()
    source_modified_at = datetime(2026, 5, 2, tzinfo=UTC)
    existing = CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Deleted Acme",
        source_modified_at=source_modified_at,
        is_active=False,
        is_deleted=True,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotCompanyWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    result = await writer.apply_single(
        company=replace(
            _normalized_company(external_id="123", name="Restored Acme"),
            source_modified_at=source_modified_at,
        ),
        owner_map={},
    )

    assert result.companies_created == 0
    assert result.companies_reactivated == 1
    assert result.companies_skipped_stale == 0
    assert existing.is_deleted is False
    assert existing.is_active is True
    assert existing.name == "Restored Acme"
    session.add.assert_not_called()
