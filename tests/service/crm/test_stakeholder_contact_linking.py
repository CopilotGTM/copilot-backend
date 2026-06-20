"""Tests for stakeholder-to-contact linking in StakeholderWriteService."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

ORG_ID = uuid4()
USER_ID = uuid4()
INTEGRATION_ID = uuid4()


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def first(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def all(self):
        return self._value if isinstance(self._value, list) else []


class FakeSession:
    """Session that returns canned results based on query content."""

    def __init__(
        self,
        org_domains: list[str] | None = None,
        existing_contact=None,
        internal_integration=None,
    ):
        self.org_domains = org_domains
        self.existing_contact = existing_contact
        self.internal_integration = internal_integration
        self.added: list = []
        self.flushed = False
        self.committed = False

    async def execute(self, stmt):
        compiled = str(stmt).lower()
        if "organisations" in compiled or "domain" in compiled:
            return FakeScalarResult(self.org_domains)
        if "crm_contacts" in compiled:
            return FakeScalarResult(self.existing_contact)
        if "knowledge_integrations" in compiled:
            return FakeScalarResult(self.internal_integration)
        return FakeScalarResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass

    async def rollback(self):
        pass


# ---------------------------------------------------------------------------
# Tests: _ensure_contact_for_stakeholder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_contact_links_existing_contact() -> None:
    """When a CRMContact exists for the email, return its contact_id."""
    existing_contact = SimpleNamespace(contact_id=uuid4(), email="buyer@customer.com")
    session = FakeSession(existing_contact=existing_contact)
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]

    contact_id = await service._ensure_contact_for_stakeholder(
        email="buyer@customer.com",
        name="Buyer Name",
        job_title="VP Sales",
        phone_number=None,
    )

    assert contact_id == existing_contact.contact_id
    assert not any(isinstance(obj, CRMContact) for obj in session.added)


@pytest.mark.asyncio
async def test_ensure_contact_creates_new_contact_when_missing() -> None:
    """When no CRMContact exists, create one under the internal integration."""
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    session = FakeSession(
        existing_contact=None,
        internal_integration=internal_integration,
    )
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]

    contact_id = await service._ensure_contact_for_stakeholder(
        email="new@customer.com",
        name="New Person",
        job_title="Engineer",
        phone_number="+1234567890",
    )

    assert contact_id is not None
    contacts = [obj for obj in session.added if isinstance(obj, CRMContact)]
    assert len(contacts) == 1
    contact = contacts[0]
    assert contact.email == "new@customer.com"
    assert contact.full_name == "New Person"
    assert contact.job_title == "Engineer"
    assert contact.phone == "+1234567890"
    assert contact.integration_id == INTEGRATION_ID


@pytest.mark.asyncio
async def test_ensure_contact_returns_none_when_no_internal_integration() -> None:
    """When internal CRM integration doesn't exist, return None."""
    session = FakeSession(existing_contact=None, internal_integration=None)
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]

    contact_id = await service._ensure_contact_for_stakeholder(
        email="nobody@nowhere.com",
        name="Nobody",
        job_title=None,
        phone_number=None,
    )

    assert contact_id is None
    assert not any(isinstance(obj, CRMContact) for obj in session.added)


# ---------------------------------------------------------------------------
# Tests: _ensure_deal_participant
# ---------------------------------------------------------------------------

DEAL_ID = uuid4()


class DealParticipantFakeSession(FakeSession):
    """Extends FakeSession with deal and participant lookup support."""

    def __init__(self, *, internal_integration=None, deal=None, existing_participant=None):
        super().__init__(internal_integration=internal_integration)
        self.deal = deal
        self.existing_participant = existing_participant

    async def execute(self, stmt):
        compiled = str(stmt).lower()
        if "crm_deal_participants" in compiled:
            return FakeScalarResult(self.existing_participant)
        if "crm_deals" in compiled:
            return FakeScalarResult(self.deal)
        return await super().execute(stmt)

    async def get(self, model_class, pk):
        if self.deal and pk == self.deal.deal_id:
            return self.deal
        return None


@pytest.mark.asyncio
async def test_ensure_deal_participant_creates_for_internal_deal() -> None:
    """Create CRMDealParticipant when deal belongs to internal CRM."""
    from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant

    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    deal = SimpleNamespace(
        deal_id=DEAL_ID,
        integration_id=INTEGRATION_ID,
        organisation_id=ORG_ID,
    )
    session = DealParticipantFakeSession(
        internal_integration=internal_integration,
        deal=deal,
    )
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    contact_id = uuid4()
    participant = await service._ensure_deal_participant(
        deal_id=DEAL_ID,
        contact_id=contact_id,
        email="buyer@customer.com",
        name="Buyer Name",
    )

    assert participant is not None
    participants = [obj for obj in session.added if isinstance(obj, CRMDealParticipant)]
    assert len(participants) == 1
    assert participant is participants[0]
    assert participant.related_email == "buyer@customer.com"
    assert participant.related_local_id == contact_id


@pytest.mark.asyncio
async def test_ensure_deal_participant_creates_for_external_deal() -> None:
    """Manual stakeholders on external deals get app-owned participants."""
    from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant

    external_integration_id = uuid4()
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    deal = SimpleNamespace(
        deal_id=DEAL_ID,
        integration_id=external_integration_id,
        organisation_id=ORG_ID,
    )
    session = DealParticipantFakeSession(
        internal_integration=internal_integration,
        deal=deal,
    )
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    participant = await service._ensure_deal_participant(
        deal_id=DEAL_ID,
        contact_id=uuid4(),
        email="buyer@customer.com",
        name="Buyer Name",
    )

    assert participant is not None
    participants = [obj for obj in session.added if isinstance(obj, CRMDealParticipant)]
    assert len(participants) == 1
    assert participant is participants[0]
    assert participant.integration_id == INTEGRATION_ID


@pytest.mark.asyncio
async def test_ensure_deal_participant_skips_when_already_exists() -> None:
    """Don't create duplicate participant."""
    from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant

    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    deal = SimpleNamespace(
        deal_id=DEAL_ID,
        integration_id=INTEGRATION_ID,
        organisation_id=ORG_ID,
    )
    existing_participant = CRMDealParticipant(
        organisation_id=ORG_ID,
        integration_id=INTEGRATION_ID,
        participant_id=uuid4(),
        deal_id=DEAL_ID,
        participant_type="contact",
        related_email="buyer@customer.com",
        related_name="Buyer Name",
        approval_status="APPROVED",
        participant_source="MANUAL",
    )
    session = DealParticipantFakeSession(
        internal_integration=internal_integration,
        deal=deal,
        existing_participant=existing_participant,
    )
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    participant = await service._ensure_deal_participant(
        deal_id=DEAL_ID,
        contact_id=uuid4(),
        email="buyer@customer.com",
        name="Buyer Name",
    )

    assert participant is existing_participant
    participants = [obj for obj in session.added if isinstance(obj, CRMDealParticipant)]
    assert len(participants) == 0


# ---------------------------------------------------------------------------
# Tests: _soft_delete_internal_participant
# ---------------------------------------------------------------------------


class SoftDeleteFakeSession(FakeSession):
    """Extends FakeSession with participant lookup for soft-delete tests."""

    def __init__(self, *, internal_integration=None, participant=None):
        super().__init__(internal_integration=internal_integration)
        self.participant = participant

    async def execute(self, stmt):
        compiled = str(stmt).lower()
        if "crm_deal_participants" in compiled:
            return FakeScalarResult(self.participant)
        return await super().execute(stmt)


@pytest.mark.asyncio
async def test_soft_delete_internal_participant() -> None:
    """Removing a stakeholder should soft-delete the matching internal CRM participant."""
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    participant = SimpleNamespace(
        participant_id=uuid4(),
        deal_id=DEAL_ID,
        integration_id=INTEGRATION_ID,
        related_email="buyer@customer.com",
        is_deleted=False,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = SoftDeleteFakeSession(
        internal_integration=internal_integration,
        participant=participant,
    )
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    await service._soft_delete_internal_participant(DEAL_ID, "buyer@customer.com")

    assert participant.is_deleted is True


@pytest.mark.asyncio
async def test_soft_delete_skips_when_no_participant() -> None:
    """No error when there's no matching participant to delete."""
    internal_integration = SimpleNamespace(integration_id=INTEGRATION_ID)
    session = SoftDeleteFakeSession(
        internal_integration=internal_integration,
        participant=None,
    )
    service = StakeholderWriteService(session, ORG_ID, USER_ID)  # type: ignore[arg-type]

    # Should not raise
    await service._soft_delete_internal_participant(DEAL_ID, "nobody@nowhere.com")
