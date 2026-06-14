from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.associations import (
    CRMAssociationCategory,
    CRMObjectAssociation,
    CRMObjectType,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMDealStageHistory,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.source_mappings import CRMSourceObjectType
from src.service.integrations.crm.records.salesforce.deals.association_writer import (
    SalesforceDealAssociationWriter,
)
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DimensionHit,
    NormalizedSalesforceDealAssociation,
    NormalizedSalesforceDealParticipant,
    NormalizedSalesforceStageTransition,
)
from src.service.integrations.crm.records.salesforce.deals.participant_writer import (
    SalesforceDealParticipantWriter,
)
from src.service.integrations.crm.records.salesforce.deals.stage_history_writer import (
    SalesforceDealStageHistoryWriter,
)


@pytest.mark.asyncio
async def test_stage_history_writer_upserts_source_mapping_for_existing_transition() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    stage_id = uuid4()
    entered_at = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    external_id = f"006A:012A:Prospecting:{entered_at.isoformat()}"
    existing = CRMDealStageHistory(
        history_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id=external_id,
        deal_id=deal_id,
        entered_at=entered_at,
    )

    session = AsyncMock()
    session.flush = AsyncMock()
    writer = SalesforceDealStageHistoryWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={external_id: existing})  # type: ignore[method-assign]

    result = await writer.apply(
        transitions=[
            NormalizedSalesforceStageTransition(
                deal_external_id="006A",
                stage_external_id="012A:Prospecting",
                from_stage_external_id=None,
                entered_at=entered_at,
                exited_at=None,
                elapsed_seconds=None,
                amount_at_stage=None,
                probability_at_stage=None,
                source="OpportunityHistory",
                data_payload={"Id": "history"},
            )
        ],
        deal_pk_map={"006A": deal_id},
        stage_map={"012A:Prospecting": DimensionHit(internal_id=stage_id)},
    )

    assert result.stage_history_updated == 1
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.STAGE_HISTORY
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.STAGE_HISTORY
    assert mapped_rows[0].local_object_id == existing.history_id


@pytest.mark.asyncio
async def test_participant_writer_upserts_source_mapping_for_existing_participant() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    external_id = "006A:contact:003A:Decision Maker"
    existing = CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id=external_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        relationship_role="Decision Maker",
    )

    session = AsyncMock()
    session.flush = AsyncMock()
    writer = SalesforceDealParticipantWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value=({external_id: existing}, {}))  # type: ignore[method-assign]

    result = await writer.apply(
        participants=[
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="003A",
                relationship_role="Decision Maker",
                is_primary=False,
                data_payload={"Id": "role"},
            )
        ],
        deal_pk_map={"006A": deal_id},
        contact_map={"003A": DimensionHit(internal_id=contact_id)},
        company_map={},
        user_map={},
    )

    assert result.participants_updated == 1
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.DEAL_PARTICIPANT
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.DEAL_PARTICIPANT
    assert mapped_rows[0].local_object_id == existing.participant_id


@pytest.mark.asyncio
async def test_participant_writer_reuses_contact_participant_with_duplicate_identity() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = SalesforceDealParticipantWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]

    result = await writer.apply(
        participants=[
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="003A",
                relationship_role="Decision Maker",
                is_primary=False,
                data_payload={"Id": "role-1"},
            ),
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="003A",
                relationship_role="Primary Contact",
                is_primary=True,
                data_payload={"Id": "role-2"},
            ),
        ],
        deal_pk_map={"006A": deal_id},
        contact_map={
            "003A": DimensionHit(
                internal_id=contact_id,
                name="Ada Lovelace",
                email="Ada@Example.com",
            )
        },
        company_map={},
        user_map={},
    )

    assert result.participants_created == 1
    assert result.participants_updated == 1
    assert session.add.call_count == 1
    created = session.add.call_args.args[0]
    assert created.relationship_role is None
    assert created.is_primary_contact is True
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert {row.local_object_id for row in mapped_rows} == {created.participant_id}
    assert {"is_primary_contact": False} in [row.mapping_metadata for row in mapped_rows]
    assert {"is_primary_contact": True} in [row.mapping_metadata for row in mapped_rows]


@pytest.mark.asyncio
async def test_participant_writer_preserves_snapshot_when_provider_relation_is_unresolved() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    external_id = "006A:contact:003A:Decision Maker"
    existing = CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id=external_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        relationship_role="Decision Maker",
        related_local_id=contact_id,
        related_name="Ada Lovelace",
        related_email="ada@example.com",
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = SalesforceDealParticipantWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value=({external_id: existing}, {}))  # type: ignore[method-assign]

    await writer.apply(
        participants=[
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="003A",
                relationship_role="Decision Maker",
                is_primary=False,
                data_payload={"Id": "role-1"},
            )
        ],
        deal_pk_map={"006A": deal_id},
        contact_map={},
        company_map={},
        user_map={},
    )

    assert existing.related_local_id == contact_id
    assert existing.related_name == "Ada Lovelace"
    assert existing.related_email == "ada@example.com"


@pytest.mark.asyncio
async def test_association_writer_upserts_source_mapping_for_existing_association() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    external_id = "006A:deal:contact:003A:OpportunityContactRole"
    existing = CRMObjectAssociation(
        association_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id=external_id,
        from_object_type=CRMObjectType.DEAL,
        from_local_id=deal_id,
        to_object_type=CRMObjectType.CONTACT,
    )

    session = AsyncMock()
    session.flush = AsyncMock()
    writer = SalesforceDealAssociationWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={external_id: existing})  # type: ignore[method-assign]

    result = await writer.apply(
        edges=[
            NormalizedSalesforceDealAssociation(
                from_external_id="006A",
                from_object_type=CRMObjectType.DEAL.value,
                to_external_id="003A",
                to_object_type=CRMObjectType.CONTACT.value,
                association_category=CRMAssociationCategory.SALESFORCE_STANDARD.value,
                association_type_id="OpportunityContactRole",
                association_label=None,
                role_label=None,
                is_primary=False,
                data_payload={"Id": "edge"},
            )
        ],
        deal_pk_map={"006A": deal_id},
        contact_map={"003A": DimensionHit(internal_id=contact_id)},
        company_map={},
    )

    assert result.associations_updated == 1
    mapping_helper.upsert_integration_mapping.assert_not_awaited()
    mapping_helper.bulk_upsert_integration_mappings.assert_awaited_once()
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert mapped_rows[0].external_object_type == CRMSourceObjectType.ASSOCIATION
    assert mapped_rows[0].local_object_type == CRMSourceObjectType.ASSOCIATION
    assert mapped_rows[0].local_object_id == existing.association_id
