"""Regression tests for participant-backed CRM stakeholder agent read tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.agents.tools.crm.list_stakeholders import list_deal_stakeholders
from src.agents.tools.crm.search_contacts import search_contacts
from src.domain.models.master_tables.user_models import UserAppRole
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.participants.stakeholder_adapter import participant_to_stakeholder_detail_dict


@pytest.fixture(autouse=True)
def authenticated_deal_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid master-database lookups while exercising stakeholder projections."""
    actor = CRMAccessActor(uuid4(), UserAppRole.ADMIN)
    monkeypatch.setattr(
        "src.agents.tools.crm.list_stakeholders.require_agent_deal_access",
        AsyncMock(return_value=actor),
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
        """Return the single ORM object expected by service read helpers."""
        return self._scalar

    def scalars(self):
        """Mirror SQLAlchemy's scalar-result chaining used by the services."""
        return self

    def all(self):
        """Return the fake collection for list-style service reads."""
        return self._scalars


class _FakeContactSearchRow:
    def __init__(self, contact: CRMContact) -> None:
        self._contact = contact
        self.score = 1.0
        self.match_field = "full_name"
        self.match_value = contact.full_name
        self.match_type = "fuzzy"

    def __getitem__(self, index: int) -> CRMContact:
        if index == 0:
            return self._contact
        raise IndexError(index)


class _SequencedSession:
    def __init__(self, results: list[_FakeResult]):
        self._results = results
        self.executed = 0
        self.statements = []

    async def execute(self, statement):
        """Return the next queued result so tests can assert query order."""
        self.executed += 1
        self.statements.append(statement)
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
    """The list tool exposes participant IDs without reviving stakeholder IDs."""
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
async def test_list_deal_stakeholders_prefers_linked_contact_profile_and_research() -> None:
    """Agent stakeholder lists should show contact-owned person data."""
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact_id = uuid4()
    participant = _participant(org_id, integration_id, deal_id, participant_id, contact_id)
    participant.related_name = "Old Stakeholder Name"
    participant.related_email = "old-stakeholder@example.com"
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        email="contact@example.com",
        full_name="Contact Owner",
        job_title="Chief Revenue Officer",
        phone="+1 555 contact",
        linkedin_url="https://linkedin.com/in/contact-owner",
        contact_research="Fresh contact research",
        properties={
            "department": "Revenue",
            "external_links": [{"platform": "other", "url": "https://x.example"}],
        },
    )
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Expansion",
    )
    session = _SequencedSession(
        [
            _FakeResult(scalar=deal),
            _FakeResult(scalars=[participant]),
            _FakeResult(scalars=[contact]),
        ]
    )
    context = _context(org_id, deal_id, session)

    result = await list_deal_stakeholders(context=context)  # type: ignore[arg-type]

    assert result[0]["contact_id"] == str(contact_id)
    assert result[0]["email"] == "contact@example.com"
    assert result[0]["name"] == "Contact Owner"
    assert result[0]["job_title"] == "Chief Revenue Officer"
    assert result[0]["phone_number"] == "+1 555 contact"
    assert result[0]["department"] == "Revenue"
    assert result[0]["linkedin_url"] == "https://linkedin.com/in/contact-owner"
    assert result[0]["external_links"] == [{"platform": "other", "url": "https://x.example"}]
    assert result[0]["stakeholder_research"] == "Fresh contact research"


def test_stakeholder_adapter_ignores_legacy_participant_profile_snapshots() -> None:
    """Participant-owned person snapshots should not leak into stakeholder reads."""
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant = _participant(org_id, integration_id, deal_id, uuid4())
    for field, value in {
        "job_title": "Stale stakeholder title",
        "phone_number": "+1 555 stale",
        "department": "Stale department",
        "linkedin_url": "https://linkedin.com/in/stale",
        "external_links": [{"url": "https://stale.example"}],
        "stakeholder_research": "Stale participant research",
        "last_researched_at": "2026-01-01",
    }.items():
        object.__setattr__(participant, field, value)

    payload = participant_to_stakeholder_detail_dict(participant)

    assert payload["job_title"] is None
    assert payload["phone_number"] is None
    assert payload["department"] is None
    assert payload["linkedin_url"] is None
    assert payload["external_links"] is None
    assert payload["stakeholder_research"] is None
    assert payload["last_researched_at"] is None


@pytest.mark.asyncio
async def test_list_deal_stakeholders_resolves_single_participant_id() -> None:
    """A one-person lookup now flows through list_deal_stakeholders."""
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
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Expansion",
    )
    session = _SequencedSession(
        [
            _FakeResult(scalar=deal),
            _FakeResult(scalar=participant),
            _FakeResult(scalar=contact),
        ]
    )
    context = _context(org_id, deal_id, session)

    result = await list_deal_stakeholders(
        context=context,  # type: ignore[arg-type]
        stakeholder_id=str(participant_id),
    )

    assert len(result) == 1
    assert result[0]["participant_id"] == str(participant_id)
    assert "stakeholder_id" not in result[0]
    assert result[0]["contact_id"] == str(contact_id)
    assert result[0]["email"] == "buyer@example.com"
    assert result[0]["decision_power"] == "DECISION_MAKER"
    assert "engagement_score" not in result[0]


@pytest.mark.asyncio
async def test_list_deal_stakeholders_single_lookup_requires_deal_context() -> None:
    """Single-stakeholder lookup still needs the immutable deal boundary."""
    org_id = uuid4()
    session = _SequencedSession([])
    context = _context(org_id, None, session)

    result = await list_deal_stakeholders(
        context=context,  # type: ignore[arg-type]
        stakeholder_id=str(uuid4()),
    )

    assert result == []
    assert session.executed == 0


@pytest.mark.asyncio
async def test_list_deal_stakeholders_filters_by_query() -> None:
    """Name queries narrow the list without adding a second detail tool."""
    org_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    matched = _participant(org_id, integration_id, deal_id, uuid4())
    matched.related_name = "Buyer Champion"
    unmatched = _participant(org_id, integration_id, deal_id, uuid4())
    unmatched.related_name = "Legal Reviewer"
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Expansion",
    )
    session = _SequencedSession(
        [_FakeResult(scalar=deal), _FakeResult(scalars=[matched, unmatched])]
    )
    context = _context(org_id, deal_id, session)

    result = await list_deal_stakeholders(
        context=context,  # type: ignore[arg-type]
        query="champion",
    )

    assert [item["participant_id"] for item in result] == [str(matched.participant_id)]


@pytest.mark.asyncio
async def test_search_contacts_uses_only_visible_deal_associations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contact search keeps participant-backed deal associations intact."""
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
        [_FakeResult(scalars=[_FakeContactSearchRow(contact)]), _FakeResult(scalars=[participant])]
    )
    context = _context(org_id, None, session)
    monkeypatch.setattr(
        "src.agents.tools.crm.search_contacts.resolve_agent_access_actor",
        AsyncMock(return_value=CRMAccessActor(uuid4(), UserAppRole.MEMBER)),
    )

    result = await search_contacts(
        context=context,  # type: ignore[arg-type]
        query="buyer",
    )

    assert result[0]["contact_id"] == str(contact_id)
    assert result[0]["deal_count"] == 1
    assert result[0]["deals"][0]["deal_id"] == str(deal_id)
    assert result[0]["deals"][0]["role"] == "DECISION_MAKER"
    assert "created_by_user_id" in str(session.statements[1])
