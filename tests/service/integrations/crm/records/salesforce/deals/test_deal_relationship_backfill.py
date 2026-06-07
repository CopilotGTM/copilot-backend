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
async def test_company_relationship_chunks_skip_deal_fk_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    captured_backfill_values: list[object] = []

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
        captured_backfill_values.append(backfill)
        return (
            DealWriteResult(),
            StageHistoryWriteResult(),
            ParticipantWriteResult(participants_created=1),
            AssociationWriteResult(),
            {"rows_updated": 0, "per_op": {}},
        )

    monkeypatch.setattr(service, "_persist", fake_persist)

    (
        participant_result,
        association_result,
        backfill_payload,
    ) = await service._sync_company_relationships(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        company_external_id_by_deal={"006DEAL": "001COMP"},
    )

    assert captured_backfill_values == [False]
    assert participant_result.participants_created == 1
    assert association_result == AssociationWriteResult()
    assert backfill_payload == {"rows_updated": 0, "per_op": {}}


@pytest.mark.asyncio
async def test_contact_role_chunks_keep_deal_fk_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    captured_backfill_values: list[object] = []

    async def fake_update_deal_relationship_snapshots(**kwargs) -> None:
        return None

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
        captured_backfill_values.append(backfill)
        assert isinstance(participant_extraction, DealParticipantExtraction)
        return (
            DealWriteResult(),
            StageHistoryWriteResult(),
            ParticipantWriteResult(participants_created=1),
            AssociationWriteResult(),
            {"rows_updated": 1, "per_op": {"primary_contact_id": 1}},
        )

    monkeypatch.setattr(
        service,
        "_update_deal_relationship_snapshots",
        fake_update_deal_relationship_snapshots,
    )
    monkeypatch.setattr(service, "_persist", fake_persist)

    _, _, _, backfill_payload = await service._persist_contact_role_rows(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        raw_contact_roles=[
            {
                "OpportunityId": "006DEAL",
                "ContactId": "003CONTACT",
                "IsPrimary": True,
            }
        ],
    )

    assert captured_backfill_values == [True]
    assert backfill_payload == {"rows_updated": 1, "per_op": {"primary_contact_id": 1}}
