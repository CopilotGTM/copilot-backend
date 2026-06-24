from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.associations import (
    CRMAssociationCategory,
    CRMObjectType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus, CRMParticipantType
from src.domain.models.organisation_tables.sync.crm.source_mappings import CRMSourceObjectType
from src.service.integrations.crm.records.salesforce.deals import deal_sync_service
from src.service.integrations.crm.records.salesforce.deals.deal_sync_service import (
    SalesforceDealRecordSyncService,
    SalesforceDealSyncContext,
)
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    AssociationWriteResult,
    DealParticipantExtraction,
    DealResolutionMaps,
    DealWriteResult,
    NormalizedSalesforceDealAssociation,
    NormalizedSalesforceDealParticipant,
    NormalizedSalesforceOpportunity,
    NormalizedSalesforceStageTransition,
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
        project_deal_created_activity_feed=False,
    ):
        captured["opportunities"] = opportunities
        captured["transitions"] = transitions
        captured["participant_extraction"] = participant_extraction
        captured["project_deal_created_activity_feed"] = project_deal_created_activity_feed
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
    assert captured["project_deal_created_activity_feed"] is False
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


@pytest.mark.asyncio
async def test_persist_loads_resolution_maps_for_current_batch_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    captured_filters: dict[str, object] = {}

    class FakeSession:
        async def commit(self) -> None:
            pass

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    async def fake_load_resolution_maps(**kwargs):
        captured_filters.update(
            {
                "pipeline_external_ids": kwargs["pipeline_external_ids"],
                "stage_external_ids": kwargs["stage_external_ids"],
                "user_external_ids": kwargs["user_external_ids"],
                "company_external_ids": kwargs["company_external_ids"],
                "contact_external_ids": kwargs["contact_external_ids"],
            }
        )
        return DealResolutionMaps()

    class FakeDealWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(
            self,
            *,
            normalized,
            maps,
            preserve_relationship_fields=False,
            project_deal_created_activity_feed=False,
        ):
            return DealWriteResult(deals_created=len(normalized))

    async def fake_load_deal_pk_map(**_kwargs):
        return {}

    class FakeStageHistoryWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, **_kwargs):
            return StageHistoryWriteResult()

    class FakeParticipantWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, **_kwargs):
            return ParticipantWriteResult()

    monkeypatch.setattr(deal_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(service, "_load_resolution_maps", fake_load_resolution_maps)
    monkeypatch.setattr(service, "_load_deal_pk_map", fake_load_deal_pk_map)
    monkeypatch.setattr(service, "_backfill_deals", _fake_backfill_deals)
    monkeypatch.setattr(deal_sync_service, "SalesforceDealWriter", FakeDealWriter)
    monkeypatch.setattr(
        deal_sync_service, "SalesforceDealStageHistoryWriter", FakeStageHistoryWriter
    )
    monkeypatch.setattr(deal_sync_service, "SalesforceDealParticipantWriter", FakeParticipantWriter)

    participant_extraction = DealParticipantExtraction(
        participants=[
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="003A",
                relationship_role=None,
                is_primary=True,
                data_payload=None,
            ),
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.COMPANY.value,
                related_external_id="001B",
                relationship_role=None,
                is_primary=True,
                data_payload=None,
            ),
        ],
        associations=[
            NormalizedSalesforceDealAssociation(
                from_external_id="006A",
                from_object_type=CRMObjectType.DEAL.value,
                to_external_id="003B",
                to_object_type=CRMObjectType.CONTACT.value,
                association_category=CRMAssociationCategory.SALESFORCE_STANDARD.value,
                association_type_id="OpportunityContactRole",
                association_label=None,
                role_label=None,
                is_primary=False,
                data_payload=None,
            )
        ],
    )

    await service._persist(
        organisation_id=organisation_id,
        integration_id=integration_id,
        opportunities=[
            _normalized_opportunity(
                pipeline_external_id="012A",
                stage_external_id="012A:Prospecting",
                owner_external_id="005A",
                created_by_user_external_id="005B",
                updated_by_user_external_id="005C",
                primary_company_external_id="001A",
                primary_contact_external_id="003A",
            )
        ],
        transitions=[
            NormalizedSalesforceStageTransition(
                deal_external_id="006A",
                stage_external_id="012A:Proposal",
                from_stage_external_id="012A:Prospecting",
                entered_at=None,
                exited_at=None,
                elapsed_seconds=None,
                amount_at_stage=None,
                probability_at_stage=None,
                source=None,
                data_payload=None,
            )
        ],
        participant_extraction=participant_extraction,
    )

    assert captured_filters == {
        "pipeline_external_ids": {"012A"},
        "stage_external_ids": {"012A:Prospecting", "012A:Proposal"},
        "user_external_ids": {"005A", "005B", "005C"},
        "company_external_ids": {"001A", "001B"},
        "contact_external_ids": {"003A", "003B"},
    }


@pytest.mark.asyncio
async def test_persist_skips_salesforce_association_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    persisted_participants: list[NormalizedSalesforceDealParticipant] = []

    class FakeSession:
        async def commit(self) -> None:
            pass

    async def fake_acquire_session(_organisation_id):
        yield FakeSession()

    async def fake_load_resolution_maps(**_kwargs):
        return DealResolutionMaps()

    async def fake_load_deal_pk_map(**_kwargs):
        return {"006A": uuid4()}

    class FakeDealWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(
            self,
            *,
            normalized,
            maps,
            preserve_relationship_fields=False,
            project_deal_created_activity_feed=False,
        ):
            return DealWriteResult()

    class FakeStageHistoryWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, **_kwargs):
            return StageHistoryWriteResult()

    class FakeParticipantWriter:
        def __init__(self, **_kwargs) -> None:
            pass

        async def apply(self, *, participants, **_kwargs):
            persisted_participants.extend(participants)
            return ParticipantWriteResult(participants_created=len(participants))

    participant = NormalizedSalesforceDealParticipant(
        deal_external_id="006A",
        participant_type=CRMParticipantType.CONTACT.value,
        related_external_id="003A",
        relationship_role="Decision Maker",
        is_primary=False,
        data_payload=None,
    )
    association = NormalizedSalesforceDealAssociation(
        from_external_id="006A",
        from_object_type=CRMObjectType.DEAL.value,
        to_external_id="003A",
        to_object_type=CRMObjectType.CONTACT.value,
        association_category=CRMAssociationCategory.SALESFORCE_STANDARD.value,
        association_type_id="OpportunityContactRole",
        association_label="Opportunity Contact Role",
        role_label="Decision Maker",
        is_primary=False,
        data_payload=None,
    )

    monkeypatch.setattr(deal_sync_service, "acquire_org_async_session", fake_acquire_session)
    monkeypatch.setattr(service, "_load_resolution_maps", fake_load_resolution_maps)
    monkeypatch.setattr(service, "_load_deal_pk_map", fake_load_deal_pk_map)
    monkeypatch.setattr(service, "_backfill_deals", _fake_backfill_deals)
    monkeypatch.setattr(deal_sync_service, "SalesforceDealWriter", FakeDealWriter)
    monkeypatch.setattr(
        deal_sync_service, "SalesforceDealStageHistoryWriter", FakeStageHistoryWriter
    )
    monkeypatch.setattr(deal_sync_service, "SalesforceDealParticipantWriter", FakeParticipantWriter)

    _, _, participant_result, association_result, _ = await service._persist(
        organisation_id=organisation_id,
        integration_id=integration_id,
        opportunities=[],
        transitions=[],
        participant_extraction=DealParticipantExtraction(
            participants=[participant],
            associations=[association],
        ),
    )

    assert persisted_participants == [participant]
    assert participant_result.participants_created == 1
    assert association_result == AssociationWriteResult()
    assert not hasattr(deal_sync_service, "SalesforceDealAssociationWriter")


@pytest.mark.asyncio
async def test_load_deal_pk_map_uses_source_mappings_and_filters_missing_local_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    live_deal_id = uuid4()
    stale_deal_id = uuid4()
    mapping_calls: list[tuple[CRMSourceObjectType, set[str] | None]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs) -> None:
            pass

        async def load_local_id_map(self, *, object_type, external_ids=None, **_kwargs):
            mapping_calls.append((object_type, set(external_ids) if external_ids else None))
            return {"006A": live_deal_id, "006STALE": stale_deal_id}

    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: [(live_deal_id,)]))
    monkeypatch.setattr(
        deal_sync_service,
        "CRMSourceMappingHelper",
        FakeMappingHelper,
        raising=False,
    )

    result = await service._load_deal_pk_map(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_ids={"006A", "006STALE"},
    )

    assert result == {"006A": live_deal_id}
    assert mapping_calls == [(CRMSourceObjectType.DEAL, {"006A", "006STALE"})]


@pytest.mark.asyncio
async def test_load_resolution_maps_uses_source_mappings_for_all_deal_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    pipeline_id = uuid4()
    stage_id = uuid4()
    user_id = uuid4()
    company_id = uuid4()
    contact_id = uuid4()
    mapping_calls: list[tuple[CRMSourceObjectType, set[str] | None]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs) -> None:
            pass

        async def load_local_id_map(self, *, object_type, external_ids=None, **_kwargs):
            mapping_calls.append((object_type, set(external_ids) if external_ids else None))
            return {
                CRMSourceObjectType.PIPELINE: {"012A": pipeline_id},
                CRMSourceObjectType.STAGE: {"012A:Prospecting": stage_id},
                CRMSourceObjectType.USER: {"005A": user_id},
                CRMSourceObjectType.COMPANY: {"001A": company_id},
                CRMSourceObjectType.CONTACT: {"003A": contact_id},
            }[object_type]

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(all=lambda: [(pipeline_id, "Enterprise", "Enterprise")]),
            SimpleNamespace(all=lambda: [(stage_id, "Prospecting", "Prospecting", pipeline_id)]),
            SimpleNamespace(all=lambda: [(user_id, "Owner", "owner@example.com")]),
            SimpleNamespace(all=lambda: [(company_id, "Acme", "acme.com")]),
            SimpleNamespace(all=lambda: [(contact_id, "Alex Morgan", "alex@example.com")]),
        ]
    )
    monkeypatch.setattr(deal_sync_service, "CRMSourceMappingHelper", FakeMappingHelper)

    maps = await service._load_resolution_maps(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        pipeline_external_ids={"012A"},
        stage_external_ids={"012A:Prospecting"},
        user_external_ids={"005A"},
        company_external_ids={"001A"},
        contact_external_ids={"003A"},
    )

    assert maps.pipeline_map["012A"].internal_id == pipeline_id
    assert maps.stage_map["012A:Prospecting"].pipeline_id == pipeline_id
    assert maps.user_map["005A"].email == "owner@example.com"
    assert maps.company_map["001A"].domain == "acme.com"
    assert maps.contact_map["003A"].name == "Alex Morgan"
    assert mapping_calls == [
        (CRMSourceObjectType.PIPELINE, {"012A"}),
        (CRMSourceObjectType.STAGE, {"012A:Prospecting"}),
        (CRMSourceObjectType.USER, {"005A"}),
        (CRMSourceObjectType.COMPANY, {"001A"}),
        (CRMSourceObjectType.CONTACT, {"003A"}),
    ]


@pytest.mark.asyncio
async def test_stage_history_sync_carries_last_deal_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    persisted_groups: list[list[str]] = []

    class FakeHistoryFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def fetch_pages(self):
            yield [
                {"OpportunityId": "006A", "StageName": "Prospecting"},
                {"OpportunityId": "006B", "StageName": "Prospecting"},
            ]
            yield [
                {"OpportunityId": "006B", "StageName": "Proposal"},
                {"OpportunityId": "006C", "StageName": "Prospecting"},
            ]

    async def fake_persist_stage_history_rows(**kwargs):
        persisted_groups.append([row["OpportunityId"] for row in kwargs["raw_stage_history"]])
        return (
            StageHistoryWriteResult(stage_history_created=1),
            0,
            {
                "rows_updated": 0,
                "per_op": {},
            },
        )

    monkeypatch.setattr(
        deal_sync_service, "SalesforceOpportunityHistoryFetcher", FakeHistoryFetcher
    )
    monkeypatch.setattr(
        service,
        "_persist_stage_history_rows",
        fake_persist_stage_history_rows,
    )

    fetched, skipped, result, backfill = await service._sync_stage_history_relationships(
        organisation_id=organisation_id,
        integration_id=integration_id,
        token_info=_token_info(organisation_id, integration_id),
        instance_url="https://example.my.salesforce.com",
        pipeline_external_id_by_deal={},
        current_stage_external_id_by_deal={},
    )

    assert persisted_groups == [["006A"], ["006B", "006B"], ["006C"]]
    assert fetched == 4
    assert skipped == 0
    assert result.stage_history_created == 3
    assert backfill == {"rows_updated": 0, "per_op": {}}


@pytest.mark.asyncio
async def test_contact_role_sync_carries_last_deal_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SalesforceDealRecordSyncService()
    organisation_id = uuid4()
    integration_id = uuid4()
    persisted_groups: list[list[str]] = []

    class FakeContactRolesFetcher:
        def __init__(self, **_kwargs) -> None:
            pass

        async def fetch_pages(self):
            yield [
                {"OpportunityId": "006A", "ContactId": "003A"},
                {"OpportunityId": "006B", "ContactId": "003B"},
            ]
            yield [
                {"OpportunityId": "006B", "ContactId": "003C"},
                {"OpportunityId": "006C", "ContactId": "003D"},
            ]

    async def fake_persist_contact_role_rows(**kwargs):
        persisted_groups.append([row["OpportunityId"] for row in kwargs["raw_contact_roles"]])
        return (
            0,
            ParticipantWriteResult(participants_created=1),
            AssociationWriteResult(),
            {"rows_updated": 0, "per_op": {}},
        )

    monkeypatch.setattr(
        deal_sync_service,
        "SalesforceOpportunityContactRolesFetcher",
        FakeContactRolesFetcher,
    )
    monkeypatch.setattr(
        service,
        "_persist_contact_role_rows",
        fake_persist_contact_role_rows,
    )

    (
        fetched,
        skipped,
        participants,
        associations,
        backfill,
    ) = await service._sync_contact_role_relationships(
        organisation_id=organisation_id,
        integration_id=integration_id,
        token_info=_token_info(organisation_id, integration_id),
        instance_url="https://example.my.salesforce.com",
    )

    assert persisted_groups == [["006A"], ["006B", "006B"], ["006C"]]
    assert fetched == 4
    assert skipped == 0
    assert participants.participants_created == 3
    assert associations == AssociationWriteResult()
    assert backfill == {"rows_updated": 0, "per_op": {}}


async def _fake_backfill_deals(**_kwargs):
    return {"rows_updated": 0, "per_op": {}}


def _token_info(organisation_id, integration_id) -> IntegrationTokenInfo:
    return IntegrationTokenInfo(
        integration_id=integration_id,
        organisation_id=organisation_id,
        access_token="token",
        refresh_token=None,
        access_token_expires_at=None,
    )


def _normalized_opportunity(
    *,
    pipeline_external_id: str,
    stage_external_id: str,
    owner_external_id: str,
    created_by_user_external_id: str,
    updated_by_user_external_id: str,
    primary_company_external_id: str,
    primary_contact_external_id: str,
) -> NormalizedSalesforceOpportunity:
    return NormalizedSalesforceOpportunity(
        external_id="006A",
        name="Deal A",
        amount=None,
        amount_home_currency=None,
        currency=None,
        deal_status=CRMDealStatus.OPEN,
        probability=None,
        predicted_amount=None,
        close_date=None,
        expected_close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        crm_url=None,
        pipeline_external_id=pipeline_external_id,
        stage_external_id=stage_external_id,
        forecast_category=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        owner_external_id=owner_external_id,
        created_by_user_external_id=created_by_user_external_id,
        updated_by_user_external_id=updated_by_user_external_id,
        primary_company_external_id=primary_company_external_id,
        primary_contact_external_id=primary_contact_external_id,
        source_created_at=None,
        source_modified_at=None,
        last_activity_at=None,
        stage_entered_at=None,
        stage_updated_at=None,
        days_to_close=None,
        associated_contact_count=None,
        associated_company_count=None,
        note_count=None,
        description=None,
        properties={},
        metrics={},
        associations_payload=None,
        data_payload={},
    )
