from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.leads.participant_service import LeadParticipantService


class _FakeOrgSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_copy_participants_to_deal_dedupes_contact_identity() -> None:
    session = _FakeOrgSession()
    organisation_id = uuid4()
    contact_id = uuid4()
    lead_id = uuid4()
    service = LeadParticipantService(
        org_session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
        master_session=SimpleNamespace(),
    )

    copied_count = await service.copy_participants_to_deal(
        lead_participants=[
            SimpleNamespace(
                lead_id=lead_id,
                participant_id=uuid4(),
                participant_type=CRMParticipantType.CONTACT,
                relationship_role="decision_maker",
                related_local_id=contact_id,
                name="Ada Lovelace",
                email="Ada@Example.com",
            ),
            SimpleNamespace(
                lead_id=lead_id,
                participant_id=uuid4(),
                participant_type=CRMParticipantType.CONTACT,
                relationship_role="primary_contact",
                related_local_id=contact_id,
                name="Ada Lovelace",
                email="ada@example.com",
            ),
        ],
        deal_id=uuid4(),
        deal_integration_id=uuid4(),
    )

    assert copied_count == 1
    participants = [obj for obj in session.added if isinstance(obj, CRMDealParticipant)]
    assert len(participants) == 1
    assert participants[0].related_local_id == contact_id
    assert participants[0].related_email == "ada@example.com"
    assert getattr(participants[0], "properties", None) is None


@pytest.mark.asyncio
async def test_copy_participants_to_deal_preserves_email_only_contact() -> None:
    session = _FakeOrgSession()
    organisation_id = uuid4()
    lead_id = uuid4()
    service = LeadParticipantService(
        org_session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
        master_session=SimpleNamespace(),
    )

    copied_count = await service.copy_participants_to_deal(
        lead_participants=[
            SimpleNamespace(
                lead_id=lead_id,
                participant_id=uuid4(),
                participant_type=CRMParticipantType.CONTACT,
                relationship_role="decision_maker",
                related_local_id=None,
                name="Grace Hopper",
                email=" Grace@Example.com ",
            )
        ],
        deal_id=uuid4(),
        deal_integration_id=uuid4(),
    )

    assert copied_count == 1
    participants = [obj for obj in session.added if isinstance(obj, CRMDealParticipant)]
    assert len(participants) == 1
    assert participants[0].participant_type == CRMParticipantType.CONTACT
    assert participants[0].related_local_id is None
    assert participants[0].related_name == "Grace Hopper"
    assert participants[0].related_email == "grace@example.com"
