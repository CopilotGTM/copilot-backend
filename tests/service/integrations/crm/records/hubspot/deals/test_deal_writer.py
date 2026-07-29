"""Regression coverage for HubSpot deal writer webhook interactions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.hubspot.deals.deal_types import (
    DealResolutionMaps,
    NormalizedHubSpotDeal,
)
from src.service.integrations.crm.records.hubspot.deals.deal_writer import HubSpotDealWriter


def _normalized_deal(*, external_id: str, name: str = "Acme renewal") -> NormalizedHubSpotDeal:
    return NormalizedHubSpotDeal(
        external_id=external_id,
        name=name,
        amount=Decimal("100.00"),
        amount_home_currency=None,
        currency="USD",
        deal_status=CRMDealStatus.OPEN,
        probability=50.0,
        predicted_amount=Decimal("50.00"),
        close_date=None,
        expected_close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        crm_url=f"https://app.hubspot.com/contacts/123/deal/{external_id}",
        pipeline_external_id=None,
        stage_external_id=None,
        forecast_category=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        owner_external_id=None,
        created_by_user_external_id=None,
        updated_by_user_external_id=None,
        primary_company_external_id=None,
        primary_contact_external_id=None,
        source_created_at=datetime(2026, 5, 1, tzinfo=UTC),
        source_modified_at=datetime(2026, 5, 2, tzinfo=UTC),
        last_activity_at=None,
        stage_entered_at=None,
        stage_updated_at=None,
        days_to_close=None,
        associated_contact_count=0,
        associated_company_count=0,
        note_count=None,
        description=None,
        properties={"dealname": name},
        metrics={},
        associations_payload={},
        data_payload={"id": external_id},
    )


def _empty_maps() -> DealResolutionMaps:
    return DealResolutionMaps(
        pipeline_map={},
        stage_map={},
        user_map={},
        company_map={},
        contact_map={},
    )


def _mock_source_mappings(writer: HubSpotDealWriter) -> MagicMock:
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    writer.source_mappings = mapping_helper
    return mapping_helper


@pytest.mark.asyncio
async def test_writer_reactivates_soft_deleted_deal_instead_of_inserting_duplicate() -> None:
    """HubSpot upserts reactivate a soft-deleted row instead of duplicating it."""
    # A delete webhook creates a soft tombstone, and a later HubSpot upsert for
    # the same external id should revive that row rather than create a duplicate.
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Deleted deal",
        is_deleted=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    mapping_helper = _mock_source_mappings(writer)

    result = await writer.apply(
        normalized=[_normalized_deal(external_id="123")], maps=_empty_maps()
    )

    assert result.deals_created == 0
    assert result.deals_updated == 1
    assert existing.is_deleted is False
    assert existing.name == "Acme renewal"
    mapping_helper.upsert_integration_mapping.assert_awaited_once()
    assert mapping_helper.upsert_integration_mapping.call_args.kwargs["external_object_type"] == (
        CRMSourceObjectType.DEAL
    )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_writer_skips_stale_active_deal_without_overwriting_local_snapshot() -> None:
    """Older fetched HubSpot payloads must not overwrite newer webhook-written rows."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Webhook latest name",
        source_modified_at=datetime(2026, 5, 3, tzinfo=UTC),
        is_deleted=False,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    stale_payload = replace(
        _normalized_deal(external_id="123", name="Older incremental name"),
        source_modified_at=datetime(2026, 5, 2, tzinfo=UTC),
    )

    result = await writer.apply(normalized=[stale_payload], maps=_empty_maps())

    assert result.deals_created == 0
    assert result.deals_updated == 0
    assert result.deals_skipped_stale == 1
    assert existing.name == "Webhook latest name"
    assert existing.source_modified_at == datetime(2026, 5, 3, tzinfo=UTC)
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_writer_does_not_project_deal_created_by_default() -> None:
    """Bulk provider sync must not backfill deal-created feed rows for old deals."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Existing deal",
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    writer._projection.upsert_deal_created = AsyncMock()  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    result = await writer.apply(
        normalized=[
            _normalized_deal(external_id="123", name="Existing update"),
            _normalized_deal(external_id="456", name="New deal"),
        ],
        maps=_empty_maps(),
    )

    assert result.deals_created == 1
    writer._projection.upsert_deal_created.assert_not_awaited()


@pytest.mark.asyncio
async def test_writer_projects_provider_created_deal_when_local_row_already_exists() -> None:
    """HubSpot creation webhooks can arrive after broad sync has created the deal."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Already synced deal",
        source_modified_at=datetime(2026, 5, 3, tzinfo=UTC),
        is_deleted=False,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    writer._projection.upsert_deal_created = AsyncMock()  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    stale_create_payload = replace(
        _normalized_deal(external_id="123", name="Provider create event"),
        source_modified_at=datetime(2026, 5, 2, tzinfo=UTC),
    )

    result = await writer.apply(
        normalized=[stale_create_payload],
        maps=_empty_maps(),
        project_deal_created_activity_feed=True,
    )

    assert result.deals_created == 0
    assert result.deals_skipped_stale == 1
    writer._projection.upsert_deal_created.assert_awaited_once_with(existing)


@pytest.mark.asyncio
async def test_writer_reactivates_soft_deleted_deal_even_when_timestamp_matches() -> None:
    """Freshness skip must not block HubSpot restore/upsert reactivation."""
    organisation_id = uuid4()
    integration_id = uuid4()
    source_modified_at = datetime(2026, 5, 2, tzinfo=UTC)
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Deleted deal",
        source_modified_at=source_modified_at,
        is_deleted=True,
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = HubSpotDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    result = await writer.apply(
        normalized=[
            replace(
                _normalized_deal(external_id="123", name="Restored deal"),
                source_modified_at=source_modified_at,
            )
        ],
        maps=_empty_maps(),
    )

    assert result.deals_created == 0
    assert result.deals_updated == 1
    assert result.deals_skipped_stale == 0
    assert existing.is_deleted is False
    assert existing.name == "Restored deal"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_provider_sync_does_not_regress_newer_copilot_activity() -> None:
    """A HubSpot snapshot cannot replace a newer linked meeting timestamp."""
    organisation_id = uuid4()
    integration_id = uuid4()
    copilot_at = datetime(2026, 7, 24, tzinfo=UTC)
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="123",
        name="Credgenics",
        last_activity_at=copilot_at,
        properties={
            "last_activity_source": "customer-defined",
            "_copilotgtm_last_activity_source": "copilot",
        },
    )
    provider_at = datetime(2026, 7, 3, tzinfo=UTC)
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = HubSpotDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"123": existing})  # type: ignore[method-assign]
    _mock_source_mappings(writer)

    await writer.apply(
        normalized=[
            replace(
                _normalized_deal(external_id="123"),
                last_activity_at=provider_at,
                properties={"last_activity_source": "customer-defined"},
            )
        ],
        maps=_empty_maps(),
    )

    assert existing.last_activity_at == copilot_at
    assert existing.properties["_copilotgtm_last_activity_source"] == "copilot"
    assert existing.properties["last_activity_source"] == "customer-defined"
