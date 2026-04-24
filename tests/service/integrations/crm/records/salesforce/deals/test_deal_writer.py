from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DealResolutionMaps,
    DimensionHit,
    NormalizedSalesforceOpportunity,
)
from src.service.integrations.crm.records.salesforce.deals.deal_writer import (
    SalesforceDealWriter,
)


def _normalized_deal(
    *, external_id: str, company_external_id: str | None
) -> NormalizedSalesforceOpportunity:
    return NormalizedSalesforceOpportunity(
        external_id=external_id,
        name=f"Deal {external_id}",
        amount=100.0,
        amount_home_currency=None,
        currency="USD",
        deal_status=CRMDealStatus.OPEN,
        probability=25.0,
        predicted_amount=25.0,
        close_date=None,
        expected_close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        crm_url=f"https://acme.my.salesforce.com/lightning/r/Opportunity/{external_id}/view",
        pipeline_external_id="012PIPE",
        stage_external_id="012PIPE:Prospecting",
        forecast_category="Pipeline",
        deal_type="New Business",
        priority=None,
        lead_source="Inbound",
        owner_external_id="005OWNER",
        created_by_user_external_id="005CREATOR",
        updated_by_user_external_id="005EDITOR",
        primary_company_external_id=company_external_id,
        primary_contact_external_id="003CONTACT",
        source_created_at=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        source_modified_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        last_activity_at=None,
        stage_entered_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        stage_updated_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        days_to_close=10,
        associated_contact_count=1,
        associated_company_count=1 if company_external_id else 0,
        note_count=None,
        description="Test deal",
        properties={"salesforce_object": "Opportunity", "raw_fields": {}},
        metrics={},
        associations_payload={"contact_count": 1},
        data_payload={"Id": external_id},
    )


@pytest.mark.asyncio
async def test_writer_creates_and_updates_deals_with_write_time_fk_resolution() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="006A",
        name="Old",
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(return_value={"006A": existing})  # type: ignore[method-assign]

    maps = DealResolutionMaps(
        pipeline_map={
            "012PIPE": DimensionHit(internal_id=uuid4(), name="Enterprise", label="Enterprise")
        },
        stage_map={
            "012PIPE:Prospecting": DimensionHit(
                internal_id=uuid4(),
                name="Prospecting",
                label="Prospecting",
                pipeline_id=uuid4(),
            )
        },
        user_map={
            "005OWNER": DimensionHit(internal_id=uuid4(), name="Owner", email="owner@example.com"),
            "005CREATOR": DimensionHit(internal_id=uuid4()),
            "005EDITOR": DimensionHit(internal_id=uuid4()),
        },
        company_map={},
        contact_map={
            "003CONTACT": DimensionHit(
                internal_id=uuid4(),
                name="Alex Morgan",
                email="alex@example.com",
            )
        },
    )

    result = await writer.apply(
        normalized=[
            _normalized_deal(external_id="006A", company_external_id="001MISSING"),
            _normalized_deal(external_id="006B", company_external_id=None),
        ],
        maps=maps,
    )

    assert result.deals_created == 1
    assert result.deals_updated == 1
    assert result.unresolved_company_link == 1
    assert existing.primary_contact_email == "alex@example.com"
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert len(created_rows) == 1
    assert created_rows[0].external_id == "006B"
    assert created_rows[0].company_id is None
