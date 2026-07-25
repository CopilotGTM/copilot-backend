"""Participant-domain tests for communication-discovered stakeholder writes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    StakeholderSource,
)
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService


@pytest.mark.asyncio
async def test_communication_discovery_caps_automatic_approval() -> None:
    """The sixth discovered contact remains pending and cannot widen history replay."""
    contact_ids = {uuid4() for _ in range(6)}
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), existing_result])
    session.flush = AsyncMock()
    service = StakeholderWriteService(session, uuid4())

    result = await service.add_communication_discovered_contacts(
        deal_id=uuid4(),
        integration_id=uuid4(),
        customer_domains=frozenset({"customer.test"}),
        contact_emails_by_id={
            contact_id: f"{index}@customer.test"
            for index, contact_id in enumerate(sorted(contact_ids, key=str))
        },
        source=StakeholderSource.EMAIL_DISCOVERY,
    )

    participants = [call.args[0] for call in session.add.call_args_list]
    assert len(participants) == 6
    assert sum(row.approval_status == "APPROVED" for row in participants) == 5
    assert sum(row.approval_status == "PENDING" for row in participants) == 1
    assert result.approved_contact_ids == {
        row.related_local_id for row in participants if row.approval_status == "APPROVED"
    }
    assert result.approved_added == 5
    assert result.pending_added == 1


@pytest.mark.asyncio
async def test_communication_discovery_rejects_unrelated_external_domains() -> None:
    """A vendor copied on a customer thread must not become a deal participant."""
    customer_id = uuid4()
    vendor_id = uuid4()
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), existing_result])
    session.flush = AsyncMock()
    service = StakeholderWriteService(session, uuid4())

    result = await service.add_communication_discovered_contacts(
        deal_id=uuid4(),
        integration_id=uuid4(),
        customer_domains=frozenset({"customer.test"}),
        contact_emails_by_id={
            customer_id: "new@customer.test",
            vendor_id: "lawyer@vendor.test",
        },
        source=StakeholderSource.EMAIL_DISCOVERY,
    )

    assert result.approved_contact_ids == {customer_id}
    assert result.skipped_domain == 1
    assert [call.args[0].related_local_id for call in session.add.call_args_list] == [customer_id]


@pytest.mark.asyncio
async def test_communication_discovery_retry_returns_existing_approved_contact() -> None:
    """A retry must retain the post-commit research signal after the row already exists."""
    contact_id = uuid4()
    existing = SimpleNamespace(
        related_local_id=contact_id,
        participant_source=StakeholderSource.EMAIL_DISCOVERY.value,
        approval_status=ApprovalStatus.APPROVED.value,
        is_deleted=False,
    )
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = [existing]
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), existing_result])
    service = StakeholderWriteService(session, uuid4())

    result = await service.add_communication_discovered_contacts(
        deal_id=uuid4(),
        integration_id=uuid4(),
        customer_domains=frozenset({"customer.test"}),
        contact_emails_by_id={contact_id: "buyer@customer.test"},
        source=StakeholderSource.EMAIL_DISCOVERY,
    )

    assert result.approved_contact_ids == {contact_id}
    assert result.approved_added == 0
    session.add.assert_not_called()
