from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.deals import deal_sync_service
from src.service.integrations.crm.records.salesforce.deals.deal_sync_service import (
    SalesforceDealRecordSyncService,
    SalesforceDealSyncContext,
)
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    AssociationWriteResult,
    DealParticipantExtraction,
    DealWriteResult,
    ParticipantWriteResult,
    StageHistoryWriteResult,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo


@pytest.mark.asyncio
async def test_persist_single_opportunity_reuses_deal_writer_without_relationship_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    captured: dict[str, object] = {}

    async def fake_persist(
        *,
        organisation_id,
        integration_id,
        opportunities,
        transitions,
        participant_extraction,
    ):
        captured["opportunities"] = opportunities
        captured["transitions"] = transitions
        captured["participant_extraction"] = participant_extraction
        return (
            DealWriteResult(deals_created=1, unresolved_stage_link=1),
            StageHistoryWriteResult(),
            ParticipantWriteResult(),
            AssociationWriteResult(),
            {"rows_updated": 0, "per_op": {}},
        )

    monkeypatch.setattr(service, "_persist", fake_persist)

    result = await service.persist_single_opportunity(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        raw_opportunity={
            "Id": "006xx0000000001AAA",
            "Name": "Deal",
            "StageName": "Prospecting",
        },
        instance_url="https://acme.my.salesforce.com",
    )

    assert result["deals_created"] == 1
    assert result["deals_updated"] == 0
    assert result["unresolved_stage_link"] == 1
    assert captured["transitions"] == []
    participant_extraction = captured["participant_extraction"]
    assert isinstance(participant_extraction, DealParticipantExtraction)
    assert participant_extraction.participants == []
    assert participant_extraction.associations == []


@pytest.mark.asyncio
async def test_run_persists_opportunities_in_chunks_before_relationships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    events: list[tuple[str, object]] = []

    class FakeOpportunitiesFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def fetch_pages(self):
            yield (
                [
                    {"Id": "006A", "Name": "Deal A", "StageName": "Prospecting"},
                    {"Id": "006B", "Name": "Deal B", "StageName": "Prospecting"},
                ],
                ["Id", "Name", "StageName"],
            )
            yield ([{"Id": "006C", "Name": "Deal C", "StageName": "Prospecting"}], None)

    class FakeHistoryFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def fetch_pages(self):
            events.append(("history_started", None))
            yield []

    class FakeContactRolesFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def fetch_pages(self):
            events.append(("roles_started", None))
            yield []

    async def fake_load_context(*_args, **_kwargs):
        return SalesforceDealSyncContext(
            token_info=IntegrationTokenInfo(
                integration_id=integration_id,
                organisation_id=organisation_id,
                access_token="token",
                refresh_token=None,
                access_token_expires_at=None,
            ),
            instance_url="https://example.my.salesforce.com",
        )

    async def fake_persist(
        *,
        organisation_id,
        integration_id,
        opportunities,
        transitions,
        participant_extraction,
        backfill=True,
        preserve_relationship_fields=False,
    ):
        events.append(("persist", [opportunity.external_id for opportunity in opportunities]))
        assert transitions == []
        assert participant_extraction.participants == []
        return (
            DealWriteResult(deals_created=len(opportunities)),
            StageHistoryWriteResult(),
            ParticipantWriteResult(),
            AssociationWriteResult(),
            {"rows_updated": 0, "per_op": {}},
        )

    monkeypatch.setattr(deal_sync_service, "_DEAL_PERSIST_CHUNK_SIZE", 2)
    monkeypatch.setattr(
        deal_sync_service, "SalesforceOpportunitiesFetcher", FakeOpportunitiesFetcher
    )
    monkeypatch.setattr(
        deal_sync_service, "SalesforceOpportunityHistoryFetcher", FakeHistoryFetcher
    )
    monkeypatch.setattr(
        deal_sync_service,
        "SalesforceOpportunityContactRolesFetcher",
        FakeContactRolesFetcher,
    )
    monkeypatch.setattr(service, "_load_context", fake_load_context)
    monkeypatch.setattr(service, "_persist", fake_persist)

    result = await service.run(organisation_id=organisation_id, integration_id=integration_id)

    assert events == [
        ("persist", ["006A", "006B"]),
        ("persist", ["006C"]),
        ("history_started", None),
        ("roles_started", None),
    ]
    assert result.fetched_opportunities == 3
    assert result.property_list_size == 3
    assert result.deals_created == 3
