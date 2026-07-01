from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.agents.tools.crm.get_stakeholder_details import get_stakeholder_details
from src.agents.tools.crm.list_stakeholders import list_deal_stakeholders
from src.agents.tools.crm.search_contacts import search_contacts
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)


class _AsyncSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


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
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        return self._results.pop(0)


def _context(org_id, deal_id, session):
    return SimpleNamespace(
        organisation_id=org_id,
        deal_id=deal_id,
        acquire_session=lambda: _AsyncSessionContext(session),
    )


def _participant(org_id, integration_id, deal_id, participant_id, contact_id=None):
    return CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_email="buyer@example.com",
        related_name="Buyer Snapshot",
        decision_power="DECISION_MAKER",
        stance="SUPPORTER",
        temperature="WARM",
        participant_source="CRM",
        approval_status="APPROVED",
    )


@pytest.mark.asyncio
async def test_list_deal_stakeholders_returns_participant_ids() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    participant = _participant(org_id, integration_id, deal_id, participant_id)
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Expansion",
    )
    session = _SequencedSession([_FakeResult(scalar=deal), _FakeResult(scalars=[participant])])
    context = _context(org_id, deal_id, session)

    result = await list_deal_stakeholders(context=context)  # type: ignore[arg-type]

    assert result[0]["participant_id"] == str(participant_id)
    assert "stakeholder_id" not in result[0]
    assert result[0]["email"] == "buyer@example.com"
    assert result[0]["name"] == "Buyer Snapshot"
    assert "engagement_score" not in result[0]


@pytest.mark.asyncio
async def test_get_stakeholder_details_resolves_participant_id() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    participant = _participant(org_id, integration_id, deal_id, participant_id, contact_id)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        email="buyer@example.com",
        full_name="Buyer Snapshot",
    )
    session = _SequencedSession([_FakeResult(scalar=participant), _FakeResult(scalar=contact)])
    context = _context(org_id, deal_id, session)

    result = await get_stakeholder_details(
        str(participant_id),
        context=context,  # type: ignore[arg-type]
    )

    assert result is not None
    assert result["participant_id"] == str(participant_id)
    assert "stakeholder_id" not in result
    assert result["contact_id"] == str(contact_id)
    assert result["email"] == "buyer@example.com"
    assert result["decision_power"] == "DECISION_MAKER"
    assert "engagement_score" not in result


@pytest.mark.asyncio
async def test_get_stakeholder_details_requires_deal_context() -> None:
    org_id = uuid4()
    session = _SequencedSession([])
    context = _context(org_id, None, session)

    result = await get_stakeholder_details(
        str(uuid4()),
        context=context,  # type: ignore[arg-type]
    )

    assert result is None
    assert session.executed == 0


@pytest.mark.asyncio
async def test_search_contacts_uses_participants_for_associated_deals() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        email="buyer@example.com",
        full_name="Buyer Contact",
    )
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Expansion",
        company_name="Acme",
    )
    participant = _participant(org_id, integration_id, deal_id, uuid4(), contact_id)
    participant.deal = deal
    session = _SequencedSession(
        [_FakeResult(scalars=[contact]), _FakeResult(scalars=[participant])]
    )
    context = _context(org_id, None, session)

    result = await search_contacts(
        context=context,  # type: ignore[arg-type]
        query="buyer",
    )

    assert result[0]["contact_id"] == str(contact_id)
    assert result[0]["deal_count"] == 1
    assert result[0]["deals"][0]["deal_id"] == str(deal_id)
    assert result[0]["deals"][0]["role"] == "DECISION_MAKER"
