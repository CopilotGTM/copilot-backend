"""Unit tests for internal CRM write services (dedupe and participants)."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from service.crm.internal.crm_company_service import CRMCompanyService, CRMCompanyServiceError
from service.crm.internal.crm_contact_service import CRMContactService, CRMContactServiceError
from service.crm.internal.deal_participant_service import (
    DealParticipantService,
)


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return _FakeScalarResult(self._value)


class _FakeSession:
    def __init__(self, *, existing=None, participant=None):
        self._existing = existing
        self._participant = participant
        self.added: list[object] = []

    async def execute(self, _stmt):
        return _FakeResult(self._existing)

    async def get(self, _model, _pk):
        return self._participant

    def add(self, obj):
        self.added.append(obj)


@dataclass
class _FakeDeal:
    deal_id: UUID
    company_external_id: str | None = None
    company_name: str | None = None
    company_domain: str | None = None


@dataclass
class _FakeParticipant:
    participant_id: UUID
    deal_id: UUID
    integration_id: UUID
    participant_type: CRMParticipantType
    related_email: str | None
    organisation_id: UUID = field(default_factory=uuid4)
    is_deleted: bool = False


@pytest.mark.asyncio
async def test_contact_email_dedupe_raises() -> None:
    session = _FakeSession(existing=object())
    service = CRMContactService(session, uuid4(), uuid4())

    with pytest.raises(CRMContactServiceError) as exc:
        await service._ensure_email_unique(uuid4(), "duplicate@example.com")

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_company_domain_dedupe_raises() -> None:
    session = _FakeSession(existing=object())
    service = CRMCompanyService(session, uuid4(), uuid4())

    with pytest.raises(CRMCompanyServiceError) as exc:
        await service._ensure_company_unique(uuid4(), "acme.com", None)

    assert exc.value.status_code == 409


def test_participant_external_id_stable_with_null_role() -> None:
    deal_id = uuid4()
    external_id = "contact-123"
    id_with_none = DealParticipantService._build_participant_external_id(
        deal_id, CRMParticipantType.CONTACT, external_id, None
    )
    id_with_empty = DealParticipantService._build_participant_external_id(
        deal_id, CRMParticipantType.CONTACT, external_id, ""
    )

    assert id_with_none == id_with_empty


@pytest.mark.asyncio
async def test_add_participants_skips_duplicate_in_request_with_null_role() -> None:
    session = _FakeSession()
    service = DealParticipantService(session, uuid4(), uuid4())

    deal_id = uuid4()
    integration_id = uuid4()
    related_local_id = uuid4()
    related_external_id = str(related_local_id)

    service._get_internal_deal = AsyncMock(
        return_value=(_FakeDeal(deal_id=deal_id), integration_id)
    )
    service._resolve_participant_entity = AsyncMock(
        return_value=(related_external_id, related_local_id, "Rita Singh", "rita@acme.com")
    )
    service._find_existing_participant = AsyncMock(return_value=None)

    created, skipped, _, _ = await service.add_participants(
        deal_id,
        [
            {"participant_type": CRMParticipantType.CONTACT, "related_id": related_local_id},
            {
                "participant_type": "contact",
                "related_id": related_local_id,
                "relationship_role": "",
            },
        ],
    )

    assert len(created) == 1
    assert len(skipped) == 1
    assert skipped[0].reason == "Duplicate participant in request"


@pytest.mark.asyncio
async def test_add_participants_skips_existing_with_null_role() -> None:
    session = _FakeSession()
    service = DealParticipantService(session, uuid4(), uuid4())

    deal_id = uuid4()
    integration_id = uuid4()
    related_local_id = uuid4()
    related_external_id = str(related_local_id)

    existing_participant = _FakeParticipant(
        participant_id=uuid4(),
        deal_id=deal_id,
        integration_id=integration_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="rita@acme.com",
    )

    service._get_internal_deal = AsyncMock(
        return_value=(_FakeDeal(deal_id=deal_id), integration_id)
    )
    service._resolve_participant_entity = AsyncMock(
        return_value=(related_external_id, related_local_id, "Rita Singh", "rita@acme.com")
    )
    service._find_existing_participant = AsyncMock(return_value=existing_participant)

    created, skipped, _, _ = await service.add_participants(
        deal_id,
        [{"participant_type": "contact", "related_id": related_local_id}],
    )

    assert len(created) == 0
    assert len(skipped) == 1
    assert skipped[0].reason == "Participant already exists"

    args = service._find_existing_participant.await_args
    assert args.args[4] == ""


@pytest.mark.asyncio
async def test_remove_participant_clears_contact_email() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    participant = _FakeParticipant(
        organisation_id=organisation_id,
        participant_id=participant_id,
        deal_id=deal_id,
        integration_id=integration_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="rita@acme.com",
    )
    session = _FakeSession(participant=participant)
    service = DealParticipantService(session, organisation_id, uuid4())

    service._get_internal_deal = AsyncMock(
        return_value=(_FakeDeal(deal_id=deal_id), integration_id)
    )

    updated, contact_changed, _ = await service.remove_participant(deal_id, participant_id)

    assert contact_changed is True
    assert updated.is_deleted is True
    assert updated.related_email is None
