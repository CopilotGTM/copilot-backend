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

REMOVED_PARTICIPANT_PROFILE_FIELDS = {
    "job_title",
    "department",
    "phone_number",
    "linkedin_url",
    "external_links",
    "stakeholder_research",
    "last_researched_at",
}


def _assert_no_removed_participant_profile_fields(row: CRMDealParticipant) -> None:
    for field_name in REMOVED_PARTICIPANT_PROFILE_FIELDS:
        assert not hasattr(row, field_name)


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
async def test_stage_history_writer_does_not_project_created_transition_by_default() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    stage_id = uuid4()
    entered_at = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = SalesforceDealStageHistoryWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._projection.upsert_stage_change = AsyncMock()  # type: ignore[method-assign]
    writer._load_existing_rows = AsyncMock(return_value={})  # type: ignore[method-assign]

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

    assert result.stage_history_created == 1
    writer._projection.upsert_stage_change.assert_not_awaited()


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
    assert existing.is_primary_contact is False
    assert existing.participant_source == "CRM"
    _assert_no_removed_participant_profile_fields(existing)


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
    assert created.relationship_role == "Decision Maker"
    assert created.is_primary_contact is True
    assert created.participant_source == "CRM"
    _assert_no_removed_participant_profile_fields(created)
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert {row.local_object_id for row in mapped_rows} == {created.participant_id}
    assert {"is_primary_contact": False} in [row.mapping_metadata for row in mapped_rows]
    assert {"is_primary_contact": True} in [row.mapping_metadata for row in mapped_rows]


@pytest.mark.asyncio
async def test_participant_writer_uses_active_identity_over_stale_mapping() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    external_id = "006A:contact:003A"
    stale = CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="stale",
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        relationship_role="Business User",
        related_local_id=contact_id,
        is_deleted=True,
    )
    active = CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="active",
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        relationship_role="Decision Maker",
        related_local_id=contact_id,
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
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=2)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(
        return_value=(
            {external_id: stale},
            {(deal_id, "contact", f"local:{contact_id}"): active},
        )
    )  # type: ignore[method-assign]

    result = await writer.apply(
        participants=[
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="003A",
                relationship_role="Economic Buyer",
                is_primary=True,
                data_payload={"Id": "role"},
            )
        ],
        deal_pk_map={"006A": deal_id},
        contact_map={},
        company_map={},
        user_map={},
    )

    assert result.participants_updated == 1
    assert stale.is_deleted is True
    assert active.is_deleted is False
    assert active.relationship_role == "Economic Buyer"
    assert active.is_primary_contact is True
    mapped_rows = mapping_helper.bulk_upsert_integration_mappings.await_args.args[0]
    assert {row.local_object_id for row in mapped_rows} == {active.participant_id}


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
async def test_participant_writer_clears_stale_primary_contact_flag() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    existing = CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="legacy",
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        relationship_role=None,
        is_primary_contact=True,
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
    writer._load_existing_rows = AsyncMock(
        return_value=({}, {(deal_id, "contact", f"local:{contact_id}"): existing})
    )  # type: ignore[method-assign]

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
        contact_map={
            "003A": DimensionHit(
                internal_id=contact_id,
                name="Ada Lovelace",
                email="ada@example.com",
            )
        },
        company_map={},
        user_map={},
    )

    assert existing.is_primary_contact is False
    assert existing.relationship_role == "Decision Maker"
    assert existing.participant_source == "CRM"


@pytest.mark.asyncio
async def test_participant_writer_marks_company_participant_as_crm_sourced_without_primary_contact() -> (
    None
):
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    company_id = uuid4()

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    writer = SalesforceDealParticipantWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.bulk_upsert_integration_mappings = AsyncMock(return_value=1)
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]

    await writer.apply(
        participants=[
            NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.COMPANY.value,
                related_external_id="001A",
                relationship_role=None,
                is_primary=True,
                data_payload={"source": "Opportunity.AccountId"},
            )
        ],
        deal_pk_map={"006A": deal_id},
        contact_map={},
        company_map={"001A": DimensionHit(internal_id=company_id)},
        user_map={},
    )

    created = session.add.call_args.args[0]
    assert created.participant_type == CRMParticipantType.COMPANY
    assert created.participant_source == "CRM"
    assert created.is_primary_contact is False


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


def test_salesforce_contact_roles_map_to_bounded_decision_power() -> None:
    writer = SalesforceDealParticipantWriter(
        session=AsyncMock(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )
    expected_by_role = {
        "Business User": "END_USER",
        "Decision Maker": "DECISION_MAKER",
        "Economic Buyer": "UNKNOWN",
        "Economic Decision Maker": "DECISION_MAKER",
        "Evaluator": "EVALUATOR",
        "Executive Sponsor": "DECISION_MAKER",
        "Influencer": "INFLUENCER",
        "Technical Buyer": "EVALUATOR",
        "Other": "UNKNOWN",
    }

    for role, expected in expected_by_role.items():
        row = writer._new_row(
            participant=NormalizedSalesforceDealParticipant(
                deal_external_id="006A",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="003A",
                relationship_role=role,
                is_primary=False,
                data_payload=None,
            ),
            deal_id=uuid4(),
            related_local_id=uuid4(),
            related_name="Buyer",
            related_email="buyer@example.com",
            now=datetime.now(UTC),
        )

        assert row.decision_power == expected


def test_primary_contact_label_is_not_stored_as_relationship_role() -> None:
    writer = SalesforceDealParticipantWriter(
        session=AsyncMock(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )

    row = writer._new_row(
        participant=NormalizedSalesforceDealParticipant(
            deal_external_id="006A",
            participant_type=CRMParticipantType.CONTACT.value,
            related_external_id="003A",
            relationship_role="Primary Contact Person",
            is_primary=True,
            data_payload=None,
        ),
        deal_id=uuid4(),
        related_local_id=uuid4(),
        related_name="Buyer",
        related_email="buyer@example.com",
        now=datetime.now(UTC),
    )

    assert row.is_primary_contact is True
    assert row.relationship_role is None


def test_salesforce_role_updates_do_not_erase_decision_power_with_primary_marker() -> None:
    writer = SalesforceDealParticipantWriter(
        session=AsyncMock(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )
    row = CRMDealParticipant(
        organisation_id=writer.organisation_id,
        integration_id=writer.integration_id,
        deal_id=uuid4(),
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=uuid4(),
        decision_power="UNKNOWN",
    )
    now = datetime.now(UTC)

    writer._apply_updates(
        row,
        participant=NormalizedSalesforceDealParticipant(
            deal_external_id="006A",
            participant_type=CRMParticipantType.CONTACT.value,
            related_external_id="003A",
            relationship_role="Decision Maker",
            is_primary=False,
            data_payload=None,
        ),
        related_local_id=row.related_local_id,
        related_name="Buyer",
        related_email="buyer@example.com",
        now=now,
    )
    assert row.decision_power == "DECISION_MAKER"

    writer._apply_updates(
        row,
        participant=NormalizedSalesforceDealParticipant(
            deal_external_id="006A",
            participant_type=CRMParticipantType.CONTACT.value,
            related_external_id="003A",
            relationship_role="primary_contact",
            is_primary=True,
            data_payload=None,
        ),
        related_local_id=row.related_local_id,
        related_name="Buyer",
        related_email="buyer@example.com",
        now=now,
    )
    assert row.decision_power == "DECISION_MAKER"
    assert row.is_primary_contact is True

    row.decision_power = "INFLUENCER"
    writer._apply_updates(
        row,
        participant=NormalizedSalesforceDealParticipant(
            deal_external_id="006A",
            participant_type=CRMParticipantType.CONTACT.value,
            related_external_id="003A",
            relationship_role="Decision Maker",
            is_primary=False,
            data_payload=None,
        ),
        related_local_id=row.related_local_id,
        related_name="Buyer",
        related_email="buyer@example.com",
        now=now,
    )
    assert row.decision_power == "INFLUENCER"


def test_salesforce_contact_reference_hints_fill_unknown_bounded_fields() -> None:
    writer = SalesforceDealParticipantWriter(
        session=AsyncMock(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )
    participant = NormalizedSalesforceDealParticipant(
        deal_external_id="006HEROX",
        participant_type=CRMParticipantType.CONTACT.value,
        related_external_id="003SHEKHAR",
        relationship_role=None,
        is_primary=True,
        data_payload=None,
        decision_power="DECISION_MAKER",
        stance="CHAMPION",
    )
    now = datetime.now(UTC)

    row = writer._new_row(
        participant=participant,
        deal_id=uuid4(),
        related_local_id=uuid4(),
        related_name="Shekhar Gupta",
        related_email="shekhar.gupta@herovired.com",
        now=now,
    )
    assert row.decision_power == "DECISION_MAKER"
    assert row.stance == "CHAMPION"

    row.decision_power = "INFLUENCER"
    row.stance = "SUPPORTER"
    writer._apply_updates(
        row,
        participant=participant,
        related_local_id=row.related_local_id,
        related_name=row.related_name,
        related_email=row.related_email,
        now=now,
    )
    assert row.decision_power == "INFLUENCER"
    assert row.stance == "SUPPORTER"


def test_salesforce_champion_role_maps_to_stance_and_preserves_role() -> None:
    writer = SalesforceDealParticipantWriter(
        session=AsyncMock(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )
    row = writer._new_row(
        participant=NormalizedSalesforceDealParticipant(
            deal_external_id="006A",
            participant_type=CRMParticipantType.CONTACT.value,
            related_external_id="003A",
            relationship_role="Champion",
            is_primary=False,
            data_payload=None,
        ),
        deal_id=uuid4(),
        related_local_id=uuid4(),
        related_name="Buyer",
        related_email="buyer@example.com",
        now=datetime.now(UTC),
    )

    assert row.relationship_role == "Champion"
    assert row.stance == "CHAMPION"


def test_salesforce_role_does_not_replace_manual_relationship_role() -> None:
    writer = SalesforceDealParticipantWriter(
        session=AsyncMock(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
    )
    row = CRMDealParticipant(
        organisation_id=writer.organisation_id,
        integration_id=writer.integration_id,
        deal_id=uuid4(),
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=uuid4(),
        relationship_role="Procurement",
        participant_source="MANUAL",
    )

    writer._apply_updates(
        row,
        participant=NormalizedSalesforceDealParticipant(
            deal_external_id="006A",
            participant_type=CRMParticipantType.CONTACT.value,
            related_external_id="003A",
            relationship_role="Decision Maker",
            is_primary=False,
            data_payload=None,
        ),
        related_local_id=row.related_local_id,
        related_name="Buyer",
        related_email="buyer@example.com",
        now=datetime.now(UTC),
    )

    assert row.relationship_role == "Procurement"
    assert row.participant_source == "MANUAL"
