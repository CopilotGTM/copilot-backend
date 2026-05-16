"""Regression coverage for HubSpot deal writer webhook interactions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
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

    result = await writer.apply(
        normalized=[_normalized_deal(external_id="123")], maps=_empty_maps()
    )

    assert result.deals_created == 0
    assert result.deals_updated == 1
    assert existing.is_deleted is False
    assert existing.name == "Acme renewal"
    session.add.assert_not_called()
