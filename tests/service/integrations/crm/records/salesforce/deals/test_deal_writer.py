"""Regression coverage for Salesforce deal writer CDC interactions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStatus,
    CRMStageState,
)
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.salesforce.deals import deal_writer
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DealResolutionMaps,
    DimensionHit,
    NormalizedSalesforceOpportunity,
)
from src.service.integrations.crm.records.salesforce.deals.deal_writer import (
    SalesforceDealWriter,
)


def _normalized_deal(
    *,
    external_id: str,
    company_external_id: str | None,
    deal_status: CRMDealStatus = CRMDealStatus.OPEN,
) -> NormalizedSalesforceOpportunity:
    return NormalizedSalesforceOpportunity(
        external_id=external_id,
        name=f"Deal {external_id}",
        amount=100.0,
        amount_home_currency=None,
        currency="USD",
        deal_status=deal_status,
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
async def test_writer_uses_lost_stage_only_for_otherwise_open_deals() -> None:
    """Resolved stage state must not override Salesforce's explicit won status."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing_open = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="006OPEN",
        name="Open deal",
        deal_status=CRMDealStatus.OPEN,
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = SalesforceDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"006OPEN": existing_open}
    )
    writer.source_mappings = MagicMock()
    writer.source_mappings.bulk_upsert_integration_mappings = AsyncMock(return_value=3)
    maps = DealResolutionMaps(
        stage_map={
            "012PIPE:Prospecting": DimensionHit(
                internal_id=uuid4(),
                stage_state=CRMStageState.LOST,
            )
        }
    )

    await writer.apply(
        normalized=[
            _normalized_deal(external_id="006OPEN", company_external_id=None),
            _normalized_deal(
                external_id="006WON",
                company_external_id=None,
                deal_status=CRMDealStatus.WON,
            ),
            _normalized_deal(external_id="006NEW", company_external_id=None),
        ],
        maps=maps,
    )

    assert existing_open.deal_status == CRMDealStatus.LOST
    created_rows = [call.args[0] for call in session.add.call_args_list]
    assert [row.deal_status for row in created_rows] == [
        CRMDealStatus.WON,
        CRMDealStatus.LOST,
    ]


@pytest.mark.asyncio
async def test_writer_creates_and_updates_deals_with_write_time_fk_resolution() -> None:
    """Salesforce deal upserts resolve provider FK values at write time."""
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
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    writer.source_mappings = mapping_helper
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
    assert not hasattr(created_rows[0], "external_id")
    assert created_rows[0].company_id is None
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert [row.external_id for row in mapped_rows] == ["006A", "006B"]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.DEAL
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.DEAL
    assert mapped_rows[0].local_object_id == existing.deal_id
    assert mapped_rows[0].mapping_metadata == {"associations_payload": {"contact_count": 1}}


@pytest.mark.asyncio
async def test_writer_does_not_project_deal_created_by_default() -> None:
    """Bulk provider sync must not backfill deal-created feed rows for old deals."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="006A",
        name="Existing",
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    writer.source_mappings = mapping_helper
    writer._projection.upsert_deal_created = AsyncMock()  # type: ignore[method-assign]
    writer._load_existing_rows = AsyncMock(return_value={"006A": existing})  # type: ignore[method-assign]

    await writer.apply(
        normalized=[
            _normalized_deal(external_id="006A", company_external_id=None),
            _normalized_deal(external_id="006B", company_external_id=None),
        ],
        maps=DealResolutionMaps(
            pipeline_map={},
            stage_map={},
            user_map={},
            company_map={},
            contact_map={},
        ),
    )

    writer._projection.upsert_deal_created.assert_not_awaited()


@pytest.mark.asyncio
async def test_writer_projects_provider_created_deal_when_local_row_already_exists() -> None:
    """Salesforce CREATE CDC can arrive after broad sync has created the deal."""
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="006A",
        name="Already synced",
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    writer = SalesforceDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._projection.upsert_deal_created = AsyncMock()  # type: ignore[method-assign]
    writer._load_existing_rows = AsyncMock(return_value={"006A": existing})  # type: ignore[method-assign]

    result = await writer.apply(
        normalized=[_normalized_deal(external_id="006A", company_external_id=None)],
        maps=DealResolutionMaps(
            pipeline_map={},
            stage_map={},
            user_map={},
            company_map={},
            contact_map={},
        ),
        project_deal_created_activity_feed=True,
    )

    assert result.deals_created == 0
    assert result.deals_updated == 1
    writer._projection.upsert_deal_created.assert_awaited_once_with(existing)


@pytest.mark.asyncio
async def test_load_existing_rows_chunks_local_id_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large Salesforce mapping lookups must stay under the query parameter cap."""
    organisation_id = uuid4()
    integration_id = uuid4()
    rows = [
        CRMDeal(
            deal_id=uuid4(),
            organisation_id=organisation_id,
            integration_id=integration_id,
            name=f"Deal {index}",
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

    writer = SalesforceDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer.source_mappings = MagicMock()
    writer.source_mappings.load_local_id_map = AsyncMock(
        return_value={f"006{index}": row.deal_id for index, row in enumerate(rows)}
    )
    monkeypatch.setattr(deal_writer, "LOCAL_ID_QUERY_CHUNK_SIZE", 2)

    existing = await writer._load_existing_rows(external_ids=None)

    assert existing == {f"006{index}": row for index, row in enumerate(rows)}
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_provider_sync_does_not_regress_newer_copilot_activity() -> None:
    """A Salesforce snapshot cannot replace a newer linked email timestamp."""
    organisation_id = uuid4()
    integration_id = uuid4()
    copilot_at = datetime(2026, 7, 24, tzinfo=UTC)
    existing = CRMDeal(
        deal_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="006DEAL",
        name="Credgenics",
        last_activity_at=copilot_at,
        properties={"last_activity_source": "copilot"},
    )
    provider_at = datetime(2026, 7, 3, tzinfo=UTC)
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = SalesforceDealWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    writer._load_existing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value={"006DEAL": existing}
    )
    writer.source_mappings = MagicMock()
    writer.source_mappings.bulk_upsert_integration_mappings = AsyncMock(return_value=1)

    await writer.apply(
        normalized=[
            replace(
                _normalized_deal(external_id="006DEAL", company_external_id=None),
                last_activity_at=provider_at,
            )
        ],
        maps=DealResolutionMaps(),
    )

    assert existing.last_activity_at == copilot_at
    assert existing.properties["last_activity_source"] == "copilot"
