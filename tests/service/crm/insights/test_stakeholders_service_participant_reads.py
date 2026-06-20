from __future__ import annotations

from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.participants.stakeholder_read_service import StakeholderReadService


class _FakeResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._scalars


class _SequencedSession:
    def __init__(self, results: list[_FakeResult]):
        self._results = results
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_list_stakeholders_reads_active_contact_participants() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Expansion",
        company_name="Acme",
        company_domain="acme.com",
    )
    participant = CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_email="buyer@example.com",
        related_name="Buyer Snapshot",
        job_title="VP Finance",
        approval_status="APPROVED",
        participant_source="CRM",
    )
    session = _SequencedSession([_FakeResult(scalar=deal), _FakeResult(scalars=[participant])])
    service = StakeholderReadService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]

    response = await service.list_stakeholders_by_deal(deal_id)

    assert response.company_domain == "acme.com"
    assert response.stakeholders[0].participant_id == participant_id
    assert response.stakeholders[0].contact_id == contact_id
    assert response.stakeholders[0].email == "buyer@example.com"
    assert response.stakeholders[0].name == "Buyer Snapshot"
    assert response.stakeholders[0].job_title == "VP Finance"


@pytest.mark.asyncio
async def test_list_stakeholders_accepts_limit_for_agent_callers() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        name="Expansion",
    )
    session = _SequencedSession([_FakeResult(scalar=deal), _FakeResult(scalars=[])])
    service = StakeholderReadService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]

    await service.list_stakeholders_by_deal(deal_id, limit=7)

    assert "LIMIT" in session.statements[-1]


@pytest.mark.asyncio
async def test_get_stakeholder_detail_resolves_participant_id_and_contact_profile() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    participant = CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_email="old@example.com",
        related_name="Old Snapshot",
        job_title="Old Title",
        approval_status="APPROVED",
        participant_source="MANUAL",
        stakeholder_research="Participant research",
    )
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        email="fresh@example.com",
        full_name="Fresh Buyer",
        job_title="CFO",
        contact_research="Fresh contact research",
    )
    session = _SequencedSession([_FakeResult(scalar=participant), _FakeResult(scalar=contact)])
    service = StakeholderReadService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]

    detail = await service.get_stakeholder_detail(deal_id, participant_id)

    assert detail is not None
    assert detail["participant_id"] == participant_id
    assert "stakeholder_id" not in detail
    assert detail["contact_id"] == contact_id
    assert detail["email"] == "fresh@example.com"
    assert detail["name"] == "Fresh Buyer"
    assert detail["job_title"] == "CFO"
    assert detail["stakeholder_research"] == "Fresh contact research"
    assert detail["engagement_history"] == []


@pytest.mark.asyncio
async def test_list_available_contacts_excludes_existing_participants() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="available@example.com",
        full_name="Available Contact",
    )
    session = _SequencedSession([_FakeResult(scalars=[contact])])
    service = StakeholderReadService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]

    contacts = await service.list_available_contacts(deal_id)

    assert contacts[0].contact_id == contact.contact_id
    statements = "\n".join(session.statements).lower()
    assert "crm_deal_participants" in statements
    assert "deal_stakeholders" not in statements
