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
from src.service.integrations.crm.records.hubspot.deals.association_writer import (
    HubSpotDealAssociationWriter,
)
from src.service.integrations.crm.records.hubspot.deals.deal_types import (
    DimensionHit,
    NormalizedDealAssociation,
    NormalizedDealParticipant,
    NormalizedStageTransition,
)
from src.service.integrations.crm.records.hubspot.deals.participant_writer import (
    HubSpotDealParticipantWriter,
)
from src.service.integrations.crm.records.hubspot.deals.stage_history_writer import (
    HubSpotDealStageHistoryWriter,
)


@pytest.mark.asyncio
async def test_stage_history_writer_upserts_source_mapping_for_existing_transition() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    stage_id = uuid4()
    entered_at = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    external_id = f"deal-1:stage-1:{entered_at.isoformat()}"
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
    writer = HubSpotDealStageHistoryWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={external_id: existing})  # type: ignore[method-assign]

    result = await writer.apply(
        transitions=[
            NormalizedStageTransition(
                deal_external_id="deal-1",
                stage_external_id="stage-1",
                from_stage_external_id=None,
                entered_at=entered_at,
                exited_at=None,
                elapsed_seconds=None,
                amount_at_stage=None,
                probability_at_stage=None,
                source="propertiesWithHistory",
            )
        ],
        deal_pk_map={"deal-1": deal_id},
        stage_map={"stage-1": DimensionHit(internal_id=stage_id)},
    )

    assert result.stage_history_updated == 1
    mapping_call = mapping_helper.upsert_integration_mapping.call_args
    assert mapping_call.kwargs["external_object_type"] == CRMSourceObjectType.STAGE_HISTORY
    assert mapping_call.kwargs["local_object_id"] == existing.history_id


@pytest.mark.asyncio
async def test_participant_writer_upserts_source_mapping_for_existing_participant() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    external_id = "deal-1:contact:contact-1:decision_maker"
    existing = CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id=external_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        relationship_role="decision_maker",
    )

    session = AsyncMock()
    session.flush = AsyncMock()
    writer = HubSpotDealParticipantWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value=({external_id: existing}, {}))  # type: ignore[method-assign]

    result = await writer.apply(
        participants=[
            NormalizedDealParticipant(
                deal_external_id="deal-1",
                participant_type=CRMParticipantType.CONTACT.value,
                related_external_id="contact-1",
                relationship_role="decision_maker",
                is_primary=False,
                data_payload={"id": "contact-1"},
            )
        ],
        deal_pk_map={"deal-1": deal_id},
        contact_map={"contact-1": DimensionHit(internal_id=contact_id)},
        company_map={},
        user_map={},
    )

    assert result.participants_updated == 1
    mapping_call = mapping_helper.upsert_integration_mapping.call_args
    assert mapping_call.kwargs["external_object_type"] == CRMSourceObjectType.DEAL_PARTICIPANT
    assert mapping_call.kwargs["local_object_id"] == existing.participant_id


@pytest.mark.asyncio
async def test_association_writer_upserts_source_mapping_for_existing_association() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    external_id = "deal-1:deal:contact:contact-1:3"
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
    writer = HubSpotDealAssociationWriter(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    mapping_helper = MagicMock()
    mapping_helper.upsert_integration_mapping = AsyncMock()
    writer.source_mappings = mapping_helper
    writer._load_existing_rows = AsyncMock(return_value={external_id: existing})  # type: ignore[method-assign]

    result = await writer.apply(
        edges=[
            NormalizedDealAssociation(
                from_external_id="deal-1",
                from_object_type=CRMObjectType.DEAL.value,
                to_external_id="contact-1",
                to_object_type=CRMObjectType.CONTACT.value,
                association_category=CRMAssociationCategory.HUBSPOT_DEFINED.value,
                association_type_id="3",
                association_label=None,
                role_label=None,
                is_primary=False,
                data_payload={"id": "contact-1"},
            )
        ],
        deal_pk_map={"deal-1": deal_id},
        contact_map={"contact-1": DimensionHit(internal_id=contact_id)},
        company_map={},
    )

    assert result.associations_updated == 1
    mapping_call = mapping_helper.upsert_integration_mapping.call_args
    assert mapping_call.kwargs["external_object_type"] == CRMSourceObjectType.ASSOCIATION
    assert mapping_call.kwargs["local_object_id"] == existing.association_id
