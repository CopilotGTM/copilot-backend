"""Regression tests for participant-backed stakeholder reads."""

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
    """Small SQLAlchemy result double for scalar and scalars read paths."""

    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        """Return one scalar row for service calls that expect optional rows."""
        return self._scalar

    def scalars(self):
        """Return self so `.all()` can mimic SQLAlchemy scalar results."""
        return self

    def all(self):
        """Return the queued scalar collection."""
        return self._scalars


class _SequencedSession:
    """Async session double that returns queued results and records statements."""

    def __init__(self, results: list[_FakeResult]):
        self._results = results
        self.statements: list[str] = []

    async def execute(self, stmt):
        """Record each query and return the next queued fake result."""
        self.statements.append(str(stmt))
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_list_stakeholders_reads_active_contact_participants() -> None:
    """List reads should merge contact-owned profile and research fields."""
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
        approval_status="APPROVED",
        participant_source="CRM",
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
    session = _SequencedSession(
        [
            _FakeResult(scalar=deal),
            _FakeResult(scalars=[participant]),
            _FakeResult(scalars=[contact]),
        ]
    )
    service = StakeholderReadService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]

    response = await service.list_stakeholders_by_deal(deal_id)

    assert response.company_domain == "acme.com"
    assert response.stakeholders[0].participant_id == participant_id
    assert response.stakeholders[0].contact_id == contact_id
    assert response.stakeholders[0].email == "fresh@example.com"
    assert response.stakeholders[0].name == "Fresh Buyer"
    assert response.stakeholders[0].job_title == "CFO"
    assert response.stakeholders[0].stakeholder_research == "Fresh contact research"


@pytest.mark.asyncio
async def test_list_stakeholders_accepts_limit_for_agent_callers() -> None:
    """Agent callers can cap list size without changing the response contract."""
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
    """Detail reads already use contact-owned profile and research fields."""
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
        approval_status="APPROVED",
        participant_source="MANUAL",
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
