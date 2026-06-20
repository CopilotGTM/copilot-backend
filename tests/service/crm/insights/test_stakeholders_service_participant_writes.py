from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.api.schemas.crm_models import CreateStakeholderRequest, UpdateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    StakeholderSource,
)
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService


class _FakeResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def first(self):
        return self._scalar

    def all(self):
        return self._scalars


class _WriteSession:
    def __init__(self, results: list[_FakeResult] | None = None):
        self._results = results or []
        self.added: list[object] = []
        self.committed = False
        self.refreshed: list[object] = []

    async def execute(self, _stmt):
        if not self._results:
            return _FakeResult()
        return self._results.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)


def _participant(
    *,
    org_id: UUID,
    deal_id: UUID,
    integration_id: UUID,
    contact_id: UUID | None = None,
    email: str = "buyer@example.com",
    status: str = ApprovalStatus.APPROVED.value,
) -> CRMDealParticipant:
    return CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=uuid4(),
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_email=email,
        related_name="Buyer",
        job_title="Old Title",
        approval_status=status,
        participant_source=StakeholderSource.MANUAL.value,
        stance="UNKNOWN",
        decision_power="UNKNOWN",
        temperature="UNKNOWN",
        synced_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_stakeholder_writes_participant_not_deal_stakeholder() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    integration_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        contact_id=contact_id,
        email="buyer@example.com",
    )
    session = _WriteSession([_FakeResult()])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_is_internal_email(_email: str) -> bool:
        return False

    async def fake_ensure_contact(**_kwargs) -> UUID:
        return contact_id

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        session.add(participant)
        return participant

    service._is_internal_email = fake_is_internal_email  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = fake_ensure_contact  # type: ignore[method-assign]
    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id,
        CreateStakeholderRequest(
            email=" Buyer@Example.com ",
            name="Buyer",
            job_title="CFO",
        ),
    )

    assert result.participant_id == participant.participant_id
    assert result.email == "buyer@example.com"
    assert result.name == "Buyer"
    assert result.job_title == "Old Title"
    assert any(isinstance(obj, CRMDealParticipant) for obj in session.added)
    assert not session.committed


@pytest.mark.asyncio
async def test_update_stakeholder_updates_contact_profile_and_participant_behavior() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=uuid4(),
        contact_id=contact_id,
    )
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@example.com",
        full_name="Old Buyer",
        job_title="Old Title",
        phone="111",
    )
    session = _WriteSession([_FakeResult(scalar=participant), _FakeResult(scalar=contact)])
    service = StakeholderWriteService(session, org_id)  # type: ignore[arg-type]

    result = await service.update_stakeholder(
        deal_id,
        participant.participant_id,
        UpdateStakeholderRequest(
            name="New Buyer",
            job_title="CFO",
            phone_number="222",
            stance="CHAMPION",
            motivation="Owns budget",
        ),
    )

    assert result.participant_id == participant.participant_id
    assert contact.full_name == "New Buyer"
    assert contact.job_title == "CFO"
    assert contact.phone == "222"
    assert participant.related_name == "New Buyer"
    assert participant.job_title == "CFO"
    assert participant.phone_number == "222"
    assert participant.stance == "CHAMPION"
    assert participant.motivation == "Owns budget"
    assert not session.committed


@pytest.mark.asyncio
async def test_approve_reject_and_remove_write_participant_status() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=uuid4(),
        status=ApprovalStatus.PENDING.value,
    )
    session = _WriteSession([_FakeResult(scalar=participant)])
    service = StakeholderWriteService(session, org_id)  # type: ignore[arg-type]

    approved = await service.approve_stakeholder(deal_id, participant.participant_id)

    assert approved.participant_id == participant.participant_id
    assert participant.approval_status == ApprovalStatus.APPROVED.value

    session._results.append(_FakeResult(scalar=participant))
    rejected = await service.reject_stakeholder(deal_id, participant.participant_id)

    assert rejected.participant_id == participant.participant_id
    assert participant.approval_status == ApprovalStatus.REJECTED.value

    participant.approval_status = ApprovalStatus.APPROVED.value
    session._results.append(_FakeResult(scalar=participant))
    removed = await service.remove_stakeholder(deal_id, participant.participant_id)

    assert removed.participant_id == participant.participant_id
    assert participant.approval_status == ApprovalStatus.REMOVED.value
    assert participant.is_deleted is True


@pytest.mark.asyncio
async def test_add_contacts_as_stakeholders_writes_participants() -> None:
    org_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=uuid4(),
        email="contact@example.com",
        full_name="Contact Buyer",
        job_title="VP Sales",
        phone="+1 555-0101",
    )
    participant = _participant(
        org_id=org_id,
        deal_id=deal_id,
        integration_id=integration_id,
        contact_id=contact.contact_id,
        email="contact@example.com",
    )
    session = _WriteSession([_FakeResult(scalars=[contact]), _FakeResult(scalars=[])])
    service = StakeholderWriteService(session, org_id, user_id=uuid4())  # type: ignore[arg-type]

    async def fake_ensure_participant(**_kwargs) -> CRMDealParticipant:
        session.add(participant)
        return participant

    service._ensure_deal_participant = fake_ensure_participant  # type: ignore[method-assign]

    result = await service.add_contacts_as_stakeholders(deal_id, [contact.contact_id])

    assert result.total_added == 1
    assert result.total_skipped == 0
    assert result.added[0].participant_id == participant.participant_id
    assert any(isinstance(obj, CRMDealParticipant) for obj in session.added)
    assert not session.committed
