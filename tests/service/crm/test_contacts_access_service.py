"""Tests for ContactsAccessService static/pure helpers."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.service.crm.access.contacts_access_service import ContactsAccessService


class _FakeResult:
    def __init__(self, *, scalar=None, first=None, scalars=None, rows=None):
        self._scalar = scalar
        self._first = first
        self._scalars = scalars or []
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def first(self):
        if self._first is None and self._scalars:
            return self._scalars[0]
        return self._first

    def all(self):
        return self._rows


class _SequencedSession:
    def __init__(self, results: list[_FakeResult]):
        self._results = results
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return self._results.pop(0)


def test_extract_domain_from_email() -> None:
    assert ContactsAccessService._extract_domain("john@acme.com") == "acme.com"
    assert ContactsAccessService._extract_domain("JANE@Example.IO") == "example.io"
    assert ContactsAccessService._extract_domain("bad-email") is None
    assert ContactsAccessService._extract_domain("") is None


def test_split_name() -> None:
    assert ContactsAccessService._split_name("John Doe") == ("John", "Doe")
    assert ContactsAccessService._split_name("Madonna") == ("Madonna", None)
    assert ContactsAccessService._split_name("Mary Jane Watson") == ("Mary", "Jane Watson")


@pytest.mark.asyncio
async def test_list_contacts_counts_deals_from_participants() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="buyer@example.com",
        full_name="Buyer Contact",
    )
    session = _SequencedSession(
        [
            _FakeResult(scalar=1),
            _FakeResult(rows=[SimpleNamespace(CRMContact=contact, deal_count=2)]),
            _FakeResult(rows=[(integration_id, IntegrationType.INTERNAL_CRM.value)]),
            _FakeResult(rows=[]),
        ]
    )
    service = ContactsAccessService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]

    rows, total = await service.list_contacts()

    assert total == 1
    assert rows[0]["deal_count"] == 2
    statements = "\n".join(session.statements).lower()
    assert "crm_deal_participants" in statements
    assert "deal_stakeholders" not in statements


@pytest.mark.asyncio
async def test_get_contact_detail_uses_participant_id_for_deal_involvement() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        full_name="Buyer Contact",
    )
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Expansion",
    )
    participant = CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        relationship_role="economic_buyer",
        stance="SUPPORTER",
        decision_power="DECISION_MAKER",
        approval_status="APPROVED",
    )
    session = _SequencedSession(
        [
            _FakeResult(scalars=[contact]),
            _FakeResult(rows=[SimpleNamespace(CRMDeal=deal, CRMDealParticipant=participant)]),
            _FakeResult(scalar=IntegrationType.INTERNAL_CRM.value),
        ]
    )
    service = ContactsAccessService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]

    detail = await service.get_contact_detail(contact_id)

    assert detail["deals"][0]["deal"] == deal
    assert detail["deals"][0]["stakeholder_id"] == participant_id
    assert detail["deals"][0]["relationship_role"] == "economic_buyer"
