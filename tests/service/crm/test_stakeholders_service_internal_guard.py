"""Tests for internal email guard in StakeholdersService.create_stakeholder."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    StakeholderSource,
)
from src.service.crm.insights.stakeholders_service import (
    StakeholdersServiceError,
)
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []


class _FakeSession:
    def __init__(self, org_domains: list[str] | None = None):
        self.org_domains = org_domains
        self.executed_statements = []
        self.added = []
        self.committed = False
        self.flushed = False
        self.rolled_back = False
        self.refreshed = []

    async def execute(self, stmt):
        compiled = str(stmt)
        self.executed_statements.append(compiled)
        if "organisations" in compiled.lower() or "domain" in compiled.lower():
            return _FakeScalarResult(self.org_domains)
        return _FakeScalarResult(None)

    async def commit(self):
        self.committed = True

    async def flush(self):
        self.flushed = True

    async def rollback(self):
        self.rolled_back = True

    def add(self, obj):
        self.added.append(obj)

    async def refresh(self, obj):
        self.refreshed.append(obj)


def _participant(
    *,
    org_id,
    deal_id,
    email: str = "buyer@customer.com",
    approval_status: str = ApprovalStatus.APPROVED.value,
    is_deleted: bool = False,
) -> CRMDealParticipant:
    return CRMDealParticipant(
        organisation_id=org_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email=email,
        related_name="Old Buyer",
        job_title="Old Title",
        participant_source=StakeholderSource.MANUAL.value,
        approval_status=approval_status,
        is_deleted=is_deleted,
    )


@pytest.mark.asyncio
async def test_is_internal_email_matches_org_domain() -> None:
    session = _FakeSession(org_domains=["acme.com", "acme.io"])
    service = StakeholderWriteService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._get_internal_domains = AsyncMock(return_value={"acme.com", "acme.io"})  # type: ignore[method-assign]

    assert await service._is_internal_email("seller@acme.com") is True
    assert await service._is_internal_email("TEAM@Acme.IO") is True


@pytest.mark.asyncio
async def test_is_internal_email_allows_external() -> None:
    session = _FakeSession(org_domains=["acme.com"])
    service = StakeholderWriteService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._get_internal_domains = AsyncMock(return_value={"acme.com"})  # type: ignore[method-assign]

    assert await service._is_internal_email("buyer@customer.com") is False
    assert await service._is_internal_email("prospect@external.org") is False


@pytest.mark.asyncio
async def test_is_internal_email_no_org_domains() -> None:
    session = _FakeSession(org_domains=None)
    service = StakeholderWriteService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._get_internal_domains = AsyncMock(return_value=set())  # type: ignore[method-assign]

    assert await service._is_internal_email("anyone@acme.com") is False


@pytest.mark.asyncio
async def test_is_internal_email_empty_or_no_at() -> None:
    session = _FakeSession(org_domains=["acme.com"])
    service = StakeholderWriteService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]

    assert await service._is_internal_email("") is False
    assert await service._is_internal_email("no-at-sign") is False


@pytest.mark.asyncio
async def test_create_stakeholder_rejects_internal_email() -> None:
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    session = _FakeSession(org_domains=["mycompany.com"])
    service = StakeholderWriteService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=True)  # type: ignore[method-assign]

    request = CreateStakeholderRequest(
        email="seller@mycompany.com",
        name="Internal Seller",
        stance=StakeholderStance.UNKNOWN,
        decision_power=DecisionPower.UNKNOWN,
    )

    with pytest.raises(StakeholdersServiceError) as exc_info:
        await service.create_stakeholder(deal_id=uuid4(), request=request)

    assert exc_info.value.status_code == 403
    assert "internal team member" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_create_stakeholder_allows_external_email() -> None:
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    session = _FakeSession(org_domains=["mycompany.com"])
    service = StakeholderWriteService(org_session=session, organisation_id=uuid4())  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._find_contact_participant = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._ensure_deal_participant = AsyncMock(
        return_value=_participant(org_id=service.organisation_id, deal_id=uuid4())
    )  # type: ignore[method-assign]
    service._get_participant_contact = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id=uuid4(),
        request=CreateStakeholderRequest(
            email="buyer@customer.com",
            name="External Buyer",
            stance=StakeholderStance.UNKNOWN,
            decision_power=DecisionPower.UNKNOWN,
        ),
    )

    assert result.email == "buyer@customer.com"


@pytest.mark.asyncio
async def test_create_stakeholder_reactivates_removed_existing_participant() -> None:
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    org_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    removed = _participant(
        org_id=org_id,
        deal_id=deal_id,
        approval_status=ApprovalStatus.REMOVED.value,
        is_deleted=True,
    )
    session = _FakeSession(org_domains=["mycompany.com"])
    service = StakeholderWriteService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = AsyncMock(return_value=contact_id)  # type: ignore[method-assign]
    service._find_contact_participant = AsyncMock(return_value=removed)  # type: ignore[method-assign]
    service._get_participant_contact = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.create_stakeholder(
        deal_id=deal_id,
        request=CreateStakeholderRequest(
            email="buyer@customer.com",
            name="Restored Buyer",
            job_title="New Title",
            stance=StakeholderStance.UNKNOWN,
            decision_power=DecisionPower.UNKNOWN,
        ),
    )

    assert result.participant_id == removed.participant_id
    assert removed.approval_status == ApprovalStatus.APPROVED.value
    assert removed.is_deleted is False
    assert removed.related_name == "Restored Buyer"
    assert removed.job_title == "New Title"
    assert removed.related_local_id == contact_id
    assert session.committed is False
    assert session.flushed is True


@pytest.mark.asyncio
async def test_create_stakeholder_duplicate_active_error_includes_conflict_data() -> None:
    from src.api.schemas.crm_models import CreateStakeholderRequest
    from src.domain.models.organisation_tables.sync.crm.stakeholders import (
        DecisionPower,
        StakeholderStance,
    )

    org_id = uuid4()
    deal_id = uuid4()
    active = _participant(org_id=org_id, deal_id=deal_id)
    session = _FakeSession(org_domains=["mycompany.com"])
    service = StakeholderWriteService(org_session=session, organisation_id=org_id)  # type: ignore[arg-type]
    service._is_internal_email = AsyncMock(return_value=False)  # type: ignore[method-assign]
    service._ensure_contact_for_stakeholder = AsyncMock(return_value=uuid4())  # type: ignore[method-assign]
    service._find_contact_participant = AsyncMock(return_value=active)  # type: ignore[method-assign]

    with pytest.raises(StakeholdersServiceError) as exc_info:
        await service.create_stakeholder(
            deal_id=deal_id,
            request=CreateStakeholderRequest(
                email="buyer@customer.com",
                stance=StakeholderStance.UNKNOWN,
                decision_power=DecisionPower.UNKNOWN,
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.data == {
        "participant_id": str(active.participant_id),
        "approval_status": "APPROVED",
        "is_deleted": False,
    }
