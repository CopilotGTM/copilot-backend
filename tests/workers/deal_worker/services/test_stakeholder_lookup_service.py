from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.workers.deal_worker.services.stakeholder_lookup_service import (
    DealLookupInfo,
    StakeholderLookupService,
)


@pytest.mark.asyncio
async def test_collect_contact_emails_uses_only_active_approved_contact_participants() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()

    approved = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="Buyer@Example.com",
        approval_status=ApprovalStatus.APPROVED.value,
    )
    pending = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="pending@example.com",
        approval_status=ApprovalStatus.PENDING.value,
    )
    deleted = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="deleted@example.com",
        approval_status=ApprovalStatus.APPROVED.value,
        is_deleted=True,
    )
    user = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.USER,
        related_email="owner@example.com",
    )
    deal = SimpleNamespace(
        deal_id=deal_id,
        primary_contact_email="primary-only@example.com",
        participants=[approved, pending, deleted, user],
    )
    info = DealLookupInfo(deal_id=deal_id, team_emails={"owner@example.com"})

    class NoQuerySession:
        async def execute(self, statement):  # noqa: ANN001
            raise AssertionError(f"unexpected query: {statement}")

    service = StakeholderLookupService(NoQuerySession(), organisation_id)  # type: ignore[arg-type]

    await service._collect_contact_emails(deal, info)  # noqa: SLF001

    assert info.contact_emails == {"buyer@example.com"}
