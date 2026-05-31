from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.integrations.crm.records.salesforce.deals.deal_sync_service import (
    SalesforceDealRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    AssociationWriteResult,
    DealParticipantExtraction,
    DealWriteResult,
    ParticipantWriteResult,
    StageHistoryWriteResult,
)


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
